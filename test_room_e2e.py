"""E2E protocol test: room multi-fight flow on the 1.7.28 server.
Two fake clients: register (with platform tags), create/join room, slot,
host STARTs, both report RESULT, host re-STARTs (rematch on the same
connection), both report again. Asserts: scores= broadcast increments,
matches rows carry region + platforms, second match finalizes (the server
half of the REMATCH ranked fix)."""
import asyncio, io, os, re, sqlite3, subprocess, sys, time

SRV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-room-e2e")
os.makedirs(WORK, exist_ok=True)
db_path = os.path.join(WORK, "users.db")
if os.path.exists(db_path):
    os.remove(db_path)

proc = subprocess.Popen([sys.executable, SRV, "--tcp-port", "9200", "--udp-port", "9201",
                         "--relay-port", "9202", "--region-code", "e2e-region"],
                        cwd=WORK, stdout=io.open(os.path.join(WORK, "server.log"), "w", encoding="utf-8"),
                        stderr=subprocess.STDOUT, text=True)
time.sleep(2.5)


class Client:
    def __init__(self, name):
        self.name = name
        self.lines = []
        self.cursor = 0  # wait_for consumes; never re-match old lines
        self.r = self.w = None

    async def connect(self):
        self.r, self.w = await asyncio.open_connection("127.0.0.1", 9200)
        asyncio.ensure_future(self._pump())

    async def _pump(self):
        try:
            while True:
                data = await self.r.readline()
                if not data:
                    break
                self.lines.append(data.decode().strip())
        except Exception:
            pass

    async def send(self, line):
        self.w.write((line + "\n").encode())
        await self.w.drain()

    async def wait_for(self, pred, timeout=5, desc=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            while self.cursor < len(self.lines):
                l = self.lines[self.cursor]
                self.cursor += 1
                if pred(l):
                    return l
            await asyncio.sleep(0.05)
        raise AssertionError(f"{self.name}: timeout waiting for {desc}; lines={self.lines[-6:]}")


async def main():
    a, b = Client("A"), Client("B")
    await a.connect()
    await b.connect()
    await a.send("REGISTER e2ehost passw0rd 1.4.1 windows")
    await b.send("REGISTER e2eguest passw0rd 1.4.1 android")
    await a.wait_for(lambda l: l.startswith("PROFILE"), desc="A profile")
    await b.wait_for(lambda l: l.startswith("PROFILE"), desc="B profile")

    await a.send("ROOM CREATE e2eroom")
    created = await a.wait_for(lambda l: l.startswith("ROOM CREATED"), desc="room created")
    code = created.split()[2]
    print("room:", code)

    await b.send(f"ROOM JOIN {code}")
    await b.wait_for(lambda l: l.startswith("ROOM JOINED"), desc="B joined")
    await a.send("ROOM SLOT A")
    await b.send("ROOM SLOT B")
    await a.wait_for(lambda l: "slot_a=e2ehost" in l and "slot_b=e2eguest" in l, desc="slots set")

    async def play_match(expect_host_wins):
        await a.send("ROOM START")
        st = await a.wait_for(lambda l: "match=" in l and "match=- " not in l, desc="match dispatched")
        mid = re.search(r"match=(\S+)", st).group(1)
        print("match:", mid)
        await a.send(f"RESULT {mid} 2 1")
        await b.send(f"RESULT {mid} 1 2")
        line = await a.wait_for(lambda l: f"scores=e2ehost:{expect_host_wins}" in l,
                                desc=f"scoreboard shows e2ehost:{expect_host_wins}")
        print("scoreboard:", re.search(r"scores=(\S+)", line).group(1))
        return mid

    m1 = await play_match(1)
    # rematch on the SAME connections — server half of the ranked fix
    m2 = await play_match(2)
    assert m1 != m2, "rematch must get a fresh match id"

    a.w.close(); b.w.close()
    return m1, m2

m1, m2 = asyncio.run(main())
time.sleep(0.5)

db = sqlite3.connect(db_path)
rows = db.execute(
    "SELECT id, p1, p2, winner, region, platform_a, platform_b, completed_at"
    " FROM matches ORDER BY created_at").fetchall()
print("matches rows:")
for r in rows:
    print("  ", r)
assert len(rows) == 2, rows
for r in rows:
    assert r[3] == "e2ehost", r          # winner recorded
    assert r[4] == "e2e-region", r       # region stamped
    assert {r[5], r[6]} == {"windows", "android"}, r  # platforms stamped
    assert r[7] is not None, r           # finalized

proc.terminate()
print("E2E OK: room create/join/slot, 2 matches incl. rematch, scoreboard 1->2, region+platforms persisted")
