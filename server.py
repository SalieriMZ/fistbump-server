"""
Fistbump matchmaking server stub for 3sx.

Implements minimal protocol to pair two 3sx clients.
Skips OAuth device-auth flow — anonymous login.

Protocol: see PROTOCOL.md
"""

import argparse
import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("fistbump")

# Data dir: FISTBUMP_DATA_DIR env > /opt/fistbump (production) > cwd (local dev).
_DATA_DIR = os.environ.get("FISTBUMP_DATA_DIR") or (
    "/opt/fistbump" if os.path.isdir("/opt/fistbump") else "."
)
DB_PATH = os.path.join(_DATA_DIR, "users.db")
TOKEN_TTL_DAYS = 30
TOKEN_SECRET_FILE = os.path.join(_DATA_DIR, "token_secret")
INTERNAL_SECRET_FILE = os.path.join(_DATA_DIR, "internal_secret")


# SF6-style rank tiers based on internal ELO (LP-equivalent).
# Each tier has 5 sub-tiers spanning 400 LP (80 per sub).
SF6_TIERS = [
    ("Rookie",   0,    1000),
    ("Iron",     1000, 1400),
    ("Bronze",   1400, 1800),
    ("Silver",   1800, 2200),
    ("Gold",     2200, 2600),
    ("Platinum", 2600, 3000),
    ("Diamond",  3000, 3400),
    ("Master",   3400, 5000),   # Master+ shows MR instead of sub-tier
    ("Legend",   5000, 99999),  # Top-of-ladder
]


PLACEMENT_MATCHES = 5  # show "Unranked" until player has this many completed matches

TIER_COLOR = {
    "Unranked": "#8b949e", "Rookie": "#9aa0a6", "Iron": "#727272",
    "Bronze": "#cd7f32", "Silver": "#c0c0c0", "Gold": "#ffd700",
    "Platinum": "#5cc8ff", "Diamond": "#b5e3ff", "Master": "#9d4dff",
    "Legend": "#ff3030",
}

BASE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="3SX Netplay — Street Fighter III: 3rd Strike rollback online matchmaking. Custom matchmaking server, SF6-style ranks, private rooms, global chat.">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: radial-gradient(ellipse at top, #1a1f2e 0%, #0d1117 60%); color: #c9d1d9; font-family: 'Segoe UI', system-ui, sans-serif; padding: 2rem 1rem; line-height: 1.6; min-height: 100vh; }}
.container {{ max-width: 1040px; margin: 0 auto; }}
.topbar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 2.5rem; }}
.brand {{ font-size: 1.1rem; font-weight: 700; color: #f0f6fc; letter-spacing: 0.04em; }}
.brand .accent {{ color: #ff3030; }}
.lang-toggle {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: .35rem .8rem; color: #c9d1d9; font-size: .85rem; cursor: pointer; font-family: inherit; }}
.lang-toggle:hover {{ background: #1c2128; }}
.hero {{ margin-bottom: 2.5rem; }}
h1 {{ font-size: 2.8rem; margin-bottom: .5rem; letter-spacing: -0.03em; line-height: 1.1; font-weight: 800; }}
h1 .accent {{ color: #ff3030; }}
.tagline {{ font-size: 1.15rem; color: #c9d1d9; margin-bottom: .5rem; }}
h2 {{ font-size: 1.4rem; margin: 2.5rem 0 1rem; color: #f0f6fc; font-weight: 700; letter-spacing: -0.01em; }}
.sub {{ color: #8b949e; margin-bottom: 2rem; font-size: .95rem; }}
.back {{ margin-bottom: 1rem; }}
.back a {{ color: #58a6ff; text-decoration: none; font-size: .9rem; }}
.stats-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
.stat {{ background: linear-gradient(180deg, #161b22 0%, #11151a 100%); border: 1px solid #30363d; border-radius: 10px; padding: 1.2rem; }}
.stat .val {{ font-size: 2.2rem; font-weight: 800; color: #f0f6fc; letter-spacing: -0.02em; }}
.stat .lbl {{ font-size: .75rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.08em; }}
table {{ width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #30363d; border-radius: 10px; overflow: hidden; }}
th {{ background: #1c2128; padding: .9rem 1rem; text-align: left; font-size: .75rem; text-transform: uppercase; letter-spacing: 0.08em; color: #8b949e; border-bottom: 1px solid #30363d; }}
td {{ padding: .9rem 1rem; border-bottom: 1px solid #21262d; font-size: .95rem; }}
tr:last-child td {{ border-bottom: none; }}
tr:hover {{ background: #1c2128; }}
.rk {{ color: #8b949e; font-weight: 700; width: 60px; }}
.usr a {{ font-weight: 600; color: #f0f6fc; text-decoration: none; }}
.usr a:hover {{ text-decoration: underline; color: #58a6ff; }}
.rnk {{ font-weight: 700; }}
.elo {{ font-family: 'Consolas', monospace; }}
.wr {{ color: #58a6ff; font-weight: 600; }}
.win {{ color: #3fb950; font-weight: 700; }}
.loss {{ color: #f85149; font-weight: 700; }}
.tie {{ color: #d29922; font-weight: 700; }}
.disp {{ color: #8b949e; font-weight: 700; }}
.empty {{ text-align: center; color: #8b949e; padding: 2.5rem; font-style: italic; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; margin: 1.5rem 0 2rem; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 1.3rem; }}
.card h3 {{ font-size: 1rem; margin-bottom: .5rem; color: #f0f6fc; }}
.card p {{ color: #8b949e; font-size: .9rem; }}
.card .icon {{ display: inline-block; width: 8px; height: 8px; background: #ff3030; border-radius: 50%; margin-bottom: .8rem; }}
.steps {{ counter-reset: step; padding-left: 0; list-style: none; }}
.steps li {{ counter-increment: step; padding: .8rem 0 .8rem 3rem; position: relative; border-bottom: 1px solid #21262d; color: #c9d1d9; font-size: .95rem; }}
.steps li:last-child {{ border-bottom: none; }}
.steps li::before {{ content: counter(step); position: absolute; left: 0; top: .75rem; width: 2rem; height: 2rem; background: #ff3030; color: white; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: .85rem; }}
.footer {{ margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid #21262d; color: #6e7681; font-size: .85rem; text-align: center; }}
.footer a {{ color: #58a6ff; text-decoration: none; }}
[data-lang="es"] [data-i18n-en], [data-lang="en"] [data-i18n-es] {{ display: none; }}
</style></head>
<body><div class="container" data-lang="en">
<div class="topbar">
  <div class="brand"><span class="accent">3SX</span> NETPLAY</div>
  <button class="lang-toggle" onclick="toggleLang()">
    <span data-i18n-en>Español</span><span data-i18n-es>English</span>
  </button>
</div>
{content}
</div>
<script>
(function() {{
  var saved = localStorage.getItem('sf3lang') || 'en';
  document.querySelector('.container').setAttribute('data-lang', saved);
}})();
function toggleLang() {{
  var c = document.querySelector('.container');
  var cur = c.getAttribute('data-lang');
  var next = cur === 'en' ? 'es' : 'en';
  c.setAttribute('data-lang', next);
  localStorage.setItem('sf3lang', next);
}}
</script>
</body></html>"""


def sf6_rank(elo: int, total_matches: int = 999) -> dict:
    """Convert internal ELO to SF6-style rank string. Unranked while < PLACEMENT_MATCHES."""
    if total_matches < PLACEMENT_MATCHES:
        remaining = PLACEMENT_MATCHES - total_matches
        return {
            "tier": "Unranked", "sub": None,
            "label": f"Unranked ({remaining} placement)", "elo": elo,
        }
    for name, lo, hi in SF6_TIERS:
        if lo <= elo < hi:
            if name in ("Master", "Legend"):
                return {"tier": name, "sub": None, "label": f"{name} {elo}", "elo": elo}
            sub = ((elo - lo) // 80) + 1
            sub = max(1, min(5, sub))
            return {"tier": name, "sub": sub, "label": f"{name} {sub}", "elo": elo}
    return {"tier": "Rookie", "sub": 1, "label": "Rookie 1", "elo": elo}


def get_token_secret() -> bytes:
    """Persistent HMAC secret for token signing. Generated on first run."""
    try:
        with open(TOKEN_SECRET_FILE, "rb") as f:
            data = f.read()
            if len(data) >= 32:
                return data
    except FileNotFoundError:
        pass
    secret = secrets.token_bytes(32)
    try:
        with open(TOKEN_SECRET_FILE, "wb") as f:
            f.write(secret)
        os.chmod(TOKEN_SECRET_FILE, 0o600)
    except Exception as e:
        log.warning("could not persist token secret: %s — using ephemeral", e)
    return secret


TOKEN_SECRET = get_token_secret()


def get_internal_secret() -> str:
    """Shared secret for inter-server /api/internal/* calls. 32-byte hex."""
    try:
        with open(INTERNAL_SECRET_FILE, "r") as f:
            s = f.read().strip()
            if len(s) >= 32:
                return s
    except FileNotFoundError:
        pass
    s = secrets.token_hex(32)
    try:
        with open(INTERNAL_SECRET_FILE, "w") as f:
            f.write(s)
        os.chmod(INTERNAL_SECRET_FILE, 0o600)
    except Exception as e:
        log.warning("could not persist internal secret: %s — using ephemeral", e)
    return s


INTERNAL_SECRET = get_internal_secret()


def hash_password(password: str, salt: bytes = None) -> str:
    """PBKDF2-SHA256 password hash. Returns 'salt$hash' hex."""
    if salt is None:
        salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt.hex() + "$" + derived.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(derived.hex(), hash_hex)
    except Exception:
        return False


def issue_token(username: str) -> tuple[str, int]:
    """Returns (token, expiry_unix). Token = username.expiry.sig (base16)."""
    expiry = int(time.time()) + TOKEN_TTL_DAYS * 24 * 3600
    payload = f"{username}.{expiry}"
    sig = hmac.new(TOKEN_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig, expiry


def verify_token(token: str) -> Optional[str]:
    """Returns username if valid, None if invalid/expired."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        username, expiry_str, sig = parts
        expiry = int(expiry_str)
        if expiry < int(time.time()):
            return None
        payload = f"{username}.{expiry}"
        expected = hmac.new(TOKEN_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return username
    except Exception:
        return None


USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,16}$")


def is_valid_username(name: str) -> bool:
    return bool(USERNAME_RE.match(name))


def gen_session_id() -> str:
    """7-char alphanumeric session id matching client `%7s` parse."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(7))


def gen_username() -> str:
    """7-char anonymous username."""
    return "u" + "".join(
        secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6)
    )


QUEUE_IDLE_TIMEOUT_S = 300  # 5 min — kick idle queued clients
MATCH_PUNCH_TIMEOUT_S = 20  # peers have 20s to UDP-punch (accept) after MATCH
FINALIZE_GRACE_S = 1800  # keep finalized match relay alive 30min for REMATCH; auto-refreshes per UDP packet in handle_relay

# Allowed client versions — latest release only. The 1.7.28 protocol added
# the platform auth field, full-length PROFILE usernames, and the ROOM STATE
# scores token; older clients half-work, so they are rejected and the
# launcher's update prompt walks them to the current release.
ALLOWED_VERSIONS = {"1.3.1", "1.7.28"}  # 1.3.1 = game protocol; 1.7.28 = launcher

ALLOWED_CHAT_SCOPES = {"general", "match", "room"}
MAX_CHAT_LEN = 256
CHAT_RATE_PER_SEC = 5.0

# Rate limits (per IP). Bumped: launcher reconnects + auth REFRESH on each
# session burn through low budgets fast during normal play.
CONNECT_RATE_LIMIT = 60   # max TCP connects per RATE_WINDOW_S
HELLO_RATE_LIMIT = 120    # max HELLO/REFRESH/LOGIN/REGISTER per RATE_WINDOW_S
QUEUE_RATE_LIMIT = 120    # max QUEUE add/remove per RATE_WINDOW_S
RATE_WINDOW_S = 60
RATE_WHITELIST_IPS = {ip.strip() for ip in os.environ.get("FISTBUMP_RATE_WHITELIST", "").split(",") if ip.strip()}

BAN_FILE = "/opt/fistbump/bans.json"  # {"1.2.3.4": "reason text", ...}

# Allow anonymous HELLO (ephemeral usernames, no DB persistence, no ELO)
ALLOW_ANON = os.environ.get("FISTBUMP_ALLOW_ANON", "0") == "1"


def _split_version_platform(rest: str):
    """Auth lines carry '<version> [platform]' — 1.7.28+ clients append their
    platform tag; older clients send the bare version."""
    parts = rest.strip().split()
    version = parts[0] if parts else ""
    platform = parts[1] if len(parts) > 1 else ""
    return version, platform


@dataclass
class ClientSession:
    sid: str
    writer: asyncio.StreamWriter
    peername: tuple
    username: str = ""
    version: str = ""
    state: str = "connected"  # connected | logged_in | queued | matched
    match_id: Optional[str] = None
    room_code: Optional[str] = None  # current room code (for CHAT room scope routing)
    udp_endpoint: Optional[tuple] = None  # (ip, port) post-NAT seen via UDP
    lan_ips: list = field(default_factory=list)  # all RFC1918 candidates the client advertised via UDP_LAN. Server intersects with the peer's list at START dispatch (/24 prefix match) to pick the right interface for LAN-direct.
    queued_at: float = 0.0  # for idle timeout
    queue_mode: str = ""  # "casual" or "ranked"
    chat_token_bucket: float = 5.0  # tokens; 5 msgs/sec capacity
    chat_last_refill: float = 0.0
    force_relay: bool = False  # client opted to disable P2P (use server relay)
    platform: str = ""  # client platform tag (windows/android/vita/...) — trailing auth field, 1.7.28+


ROOM_MAX_MEMBERS = 8
ROOM_EMPTY_TIMEOUT_S = 120  # reap rooms idle (no members) after 2 min so a
                            # post-match Soft_Reset_Sub → title attract → re-enter
                            # Network menu navigates back into the same room
                            # without the launcher having to reconnect.

@dataclass
class Room:
    code: str  # 5-char join code
    name: str
    host_sid: str
    members: list  # list of sids (host + slot players + spectators), max ROOM_MAX_MEMBERS
    created_at: float = field(default_factory=time.time)
    # Two-slot active match — host picks which two members fight next.
    # Spectators stay in members but unassigned to slots.
    slot_a: Optional[str] = None  # sid of player in slot A
    slot_b: Optional[str] = None  # sid of player in slot B
    # Per-room match settings the host can tweak before pressing START.
    settings_best_of: int = 3       # rounds-to-win
    settings_timer: int = 99        # round timer
    settings_damage: int = 1        # 0=weak, 1=normal, 2=strong
    # Current dispatched match id (None when no match in progress in this room).
    # Persists until RESULT finalizes — used to gate START spam.
    current_match_id: Optional[str] = None
    # Empty-room grace: set when last member leaves; janitor reaps after
    # ROOM_EMPTY_TIMEOUT_S so user can soft-reset post-match and rejoin code.
    empty_since: Optional[float] = None
    # Cumulative match wins per username for the lifetime of the room
    # (multi-fight scoreboard, 1.7.28). Dies with the room.
    scores: dict = field(default_factory=dict)


def gen_room_code() -> str:
    """5-char uppercase + digits join code (e.g. 'XK4P9'). Excludes confusing chars."""
    # Drop O/0/I/1 to avoid confusion when sharing codes verbally
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(5))


@dataclass
class PendingMatch:
    match_id: str
    sid_a: str
    sid_b: str
    created_at: float = field(default_factory=time.time)
    # Result reporting — both peers must agree
    result_a: Optional[tuple] = None  # (wins_a_reported, wins_b_reported) as A sees it
    result_b: Optional[tuple] = None  # same from B's perspective
    finalized: bool = False
    finalized_at: float = 0.0  # set when RESULT processed; janitor uses for linger
    started: bool = False  # both peers UDP-registered, START dispatched
    user_a: str = ""  # username at MATCH time (snapshot)
    user_b: str = ""
    is_ranked: bool = False  # affects ELO
    # Live state for web dashboard (updated by client STATE messages)
    live_state: Optional[dict] = None  # {hp1, hp2, round, char1, char2, ts}
    live_state_at: float = 0.0


class FistbumpServer:
    def __init__(self, tcp_port: int, udp_port: int, relay_port: int, host: str = "0.0.0.0", public_host: str = "127.0.0.1", upstream_url: Optional[str] = None, region_code: str = "unknown"):
        self.host = host
        self.public_host = public_host
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.relay_port = relay_port
        # If set, this server proxies auth + match results to upstream HTTP endpoint
        # instead of writing local users/matches DB. Used for edge regions sharing
        # a single source-of-truth leaderboard.
        self.upstream_url = upstream_url.rstrip("/") if upstream_url else None
        self.region_code = region_code or "unknown"  # stamped on match records
        self.sessions: dict[str, ClientSession] = {}
        # match_uuid (str) -> {"1": (ip, port) | None, "2": (ip, port) | None}
        self.relay_routes: dict[str, dict[str, tuple]] = {}
        self.queue_casual: list[str] = []  # sids waiting for casual match
        self.queue_ranked: list[str] = []  # sids waiting for ranked match (affects ELO)
        self.matches: dict[str, PendingMatch] = {}  # match_id -> PendingMatch
        self.rooms: dict[str, Room] = {}  # room_code -> Room
        self.udp_transport: Optional[asyncio.DatagramTransport] = None
        self.relay_transport = None
        # Anti-abuse state
        self.bans: dict[str, str] = {}  # ip -> reason
        self.rate_history: dict[str, dict[str, list[float]]] = {}  # ip -> {"connect": [...], "hello": [...], "queue": [...]}
        self.reject_count: dict[str, int] = {}  # ip -> count (for auto-ban escalation)
        self._load_bans()
        self._init_db()

    def _init_db(self):
        self.db = sqlite3.connect(DB_PATH)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                elo INTEGER NOT NULL DEFAULT 1000,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                last_login_at INTEGER
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id TEXT PRIMARY KEY,
                p1 TEXT NOT NULL,
                p2 TEXT NOT NULL,
                winner TEXT,
                p1_elo_delta INTEGER DEFAULT 0,
                p2_elo_delta INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                completed_at INTEGER
            )
        """)
        # Additive columns (1.7.28): where the match was played + on what.
        for _col in ("region TEXT", "platform_a TEXT", "platform_b TEXT"):
            try:
                self.db.execute(f"ALTER TABLE matches ADD COLUMN {_col}")
            except sqlite3.OperationalError:
                pass  # already migrated
        self.db.commit()
        log.info("DB ready at %s", DB_PATH)

    def db_get_user(self, username: str):
        cur = self.db.execute("SELECT username, password_hash, elo, wins, losses FROM users WHERE username=?", (username,))
        return cur.fetchone()

    def db_create_user(self, username: str, password: str):
        self.db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), int(time.time())),
        )
        self.db.commit()

    def db_touch_login(self, username: str):
        self.db.execute("UPDATE users SET last_login_at=? WHERE username=?", (int(time.time()), username))
        self.db.commit()

    def _load_bans(self):
        try:
            import json
            with open(BAN_FILE, "r") as f:
                self.bans = json.load(f)
            log.info("loaded %d bans from %s", len(self.bans), BAN_FILE)
        except FileNotFoundError:
            self.bans = {}
        except Exception as e:
            log.warning("failed to load bans: %s", e)
            self.bans = {}

    def _rate_check(self, ip: str, bucket: str, limit: int) -> bool:
        """Returns True if action is allowed, False if rate-limited."""
        if ip in RATE_WHITELIST_IPS:
            return True
        now = time.time()
        if ip not in self.rate_history:
            self.rate_history[ip] = {}
        history = self.rate_history[ip].setdefault(bucket, [])
        # Drop entries older than window
        cutoff = now - RATE_WINDOW_S
        while history and history[0] < cutoff:
            history.pop(0)
        if len(history) >= limit:
            return False
        history.append(now)
        return True

    def _register_reject(self, ip: str, reason: str):
        """Track rejected actions for auto-ban escalation."""
        self.reject_count[ip] = self.reject_count.get(ip, 0) + 1
        if self.reject_count[ip] >= 50 and ip not in self.bans:
            self.bans[ip] = f"auto-banned: {reason} (50+ rejects)"
            log.warning("AUTO-BAN ip=%s reason=%s", ip, self.bans[ip])
            self._persist_bans()

    def _persist_bans(self):
        try:
            import json
            with open(BAN_FILE, "w") as f:
                json.dump(self.bans, f, indent=2)
        except Exception as e:
            log.warning("failed to persist bans: %s", e)

    # ---------- TCP send helpers ----------

    async def send(self, session: ClientSession, line: str):
        """Send a single TCP line (newline-terminated)."""
        if not line.endswith("\n"):
            line += "\n"
        try:
            session.writer.write(line.encode("utf-8"))
            await session.writer.drain()
            log.info("→ %s: %s", session.sid, line.rstrip())
        except Exception as e:
            log.warning("send failed sid=%s: %s", session.sid, e)

    # ---------- TCP handler ----------

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"

        # Ban check
        if ip in self.bans:
            log.info("BANNED connect ip=%s reason=%s", ip, self.bans[ip])
            try:
                writer.write(f"REJECT banned: {self.bans[ip]}\n".encode())
                await writer.drain()
            except Exception:
                pass
            writer.close()
            return

        # Rate limit
        if not self._rate_check(ip, "connect", CONNECT_RATE_LIMIT):
            log.warning("RATE-LIMIT connect ip=%s", ip)
            self._register_reject(ip, "connect rate")
            try:
                writer.write(b"REJECT rate-limited\n")
                await writer.drain()
            except Exception:
                pass
            writer.close()
            return

        sid = gen_session_id()
        # ensure uniqueness
        while sid in self.sessions:
            sid = gen_session_id()

        session = ClientSession(sid=sid, writer=writer, peername=peer)
        self.sessions[sid] = session
        log.info("← connect sid=%s peer=%s", sid, peer)

        # Immediately send SESSION
        await self.send(session, f"SESSION {sid}")

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                cmd = line.decode("utf-8", errors="ignore").rstrip("\r\n")
                if not cmd:
                    continue
                log.info("← %s: %s", sid, cmd)
                await self.handle_cmd(session, cmd)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("handler error sid=%s", sid)
        finally:
            await self.cleanup_session(sid)
            log.info("disconnect sid=%s", sid)

    async def handle_cmd(self, session: ClientSession, cmd: str):
        if cmd == "HELLO" or cmd.startswith("HELLO "):
            version = cmd[6:].strip() if " " in cmd else ""
            await self.handle_hello(session, version)
        elif cmd.startswith("REGISTER "):
            await self.handle_register(session, cmd[9:])
        elif cmd.startswith("LOGIN "):
            await self.handle_login(session, cmd[6:])
        elif cmd.startswith("REFRESH "):
            rest = cmd[8:]
            parts = rest.split(" ", 1)
            token = parts[0]
            version = parts[1].strip() if len(parts) > 1 else ""
            await self.handle_refresh(session, token, version)
        elif cmd == "QUEUE add" or cmd.startswith("QUEUE add "):
            # Default to casual for backward compat (older clients sent just "QUEUE add")
            mode = cmd[10:].strip() if cmd.startswith("QUEUE add ") else "casual"
            if mode not in ("casual", "ranked"):
                mode = "casual"
            await self.handle_queue_add(session, mode)
        elif cmd == "QUEUE remove" or cmd.startswith("QUEUE remove"):
            await self.handle_queue_remove(session)
        elif cmd.startswith("DECLINE "):
            await self.handle_decline(session, cmd[8:])
        elif cmd.startswith("RESULT "):
            await self.handle_result(session, cmd[7:])
        elif cmd.startswith("ROOM "):
            await self.handle_room(session, cmd[5:])
        elif cmd.startswith("CHAT "):
            await self.handle_chat(session, cmd[5:])
        elif cmd.startswith("STATE "):
            await self.handle_state_stream(session, cmd[6:])
        elif cmd.startswith("SET "):
            await self.handle_set(session, cmd[4:])
        elif cmd.startswith("UDP_LAN "):
            await self.handle_udp_lan(session, cmd[8:])
        elif cmd.startswith("LOADING "):
            await self.handle_loading(session, cmd[8:])
        else:
            log.warning("unknown cmd sid=%s: %s", session.sid, cmd)

    async def handle_loading(self, session: ClientSession, payload: str):
        """LOADING <0|1> — client signals it is doing a blocking I/O burst
        (CPS3 character/stage asset load on slow hardware like the Vita's
        eMMC) and the peer should pause its gekko advance until cleared.
        Forward verbatim to the match peer; no-op when not in a match."""
        flag = payload.strip()
        if flag not in ("0", "1"):
            return
        if session.state != "matched" or not session.match_id:
            return
        m = self.matches.get(session.match_id)
        if not m:
            return
        peer_sid = m.sid_b if session.sid == m.sid_a else m.sid_a
        peer = self.sessions.get(peer_sid)
        if peer is None:
            return
        try:
            await self.send(peer, f"LOADING {flag}")
        except Exception:
            pass

    async def handle_udp_lan(self, session: ClientSession, payload: str):
        """UDP_LAN <ip1>,<ip2>,... — client advertises ALL its RFC1918 LAN
        candidates so same-NAT pairings get LAN-direct (no relay). Server
        intersects with the peer's list at START dispatch.

        Reject:
          - public IPs (would let peers DoS arbitrary hosts)
          - 169.254.* APIPA (link-local, only reaches same machine)
          - 172.* (Hyper-V / WSL2 / Docker virtual switches on Windows are
            in this range and don't reach the real LAN)
          - 127.* loopback"""
        candidates = []
        for raw in payload.strip().split(","):
            ip = raw.strip()
            if not ip:
                continue
            if not (ip.startswith("10.") or ip.startswith("192.168.")):
                continue
            candidates.append(ip)
        # De-dup, preserve order so client ranking (192.168 first) wins ties.
        seen = set()
        session.lan_ips = [ip for ip in candidates if not (ip in seen or seen.add(ip))]
        log.info("UDP_LAN sid=%s lan_ips=%s", session.sid, session.lan_ips)

    async def handle_set(self, session: ClientSession, payload: str):
        """SET <key> <value> — per-session client preferences."""
        parts = payload.split()
        if len(parts) != 2:
            return
        key, val = parts[0], parts[1]
        if key == "force_relay":
            session.force_relay = (val == "1")
            log.info("SET force_relay=%s sid=%s", session.force_relay, session.sid)

    async def handle_state_stream(self, session: ClientSession, payload: str):
        """STATE <match_id> <hp1> <hp2> <round> <char1> <char2>
        Live state update for web dashboard (NOT used for gameplay sync — Gekko handles that).
        """
        parts = payload.split()
        if len(parts) != 6:
            return
        match_id, hp1, hp2, rnd, char1, char2 = parts
        m = self.matches.get(match_id)
        if not m:
            return
        if session.sid not in (m.sid_a, m.sid_b):
            return
        try:
            m.live_state = {
                "hp1": int(hp1), "hp2": int(hp2), "round": int(rnd),
                "char1": int(char1), "char2": int(char2),
                "reporter": session.username,
            }
            m.live_state_at = time.time()
        except ValueError:
            return

    async def handle_chat(self, session: ClientSession, body: str) -> None:
        """CHAT <scope> <msg> — scope-routed chat with token-bucket rate limit.

        scope=general  → broadcast to all logged-in sessions
        scope=match    → send only to the two players in session.match_id
        scope=room     → send only to members of session.room_code
        """
        # Auth check — state field is the source of truth; no separate logged_in bool
        if session.state not in ("logged_in", "queued", "matched"):
            await self.send(session, "REJECT chat not logged in")
            return
        parts = body.split(" ", 1)
        if len(parts) != 2:
            await self.send(session, "REJECT chat usage: CHAT <scope> <msg>")
            return
        scope, text = parts[0].lower(), parts[1].strip()
        if scope not in ALLOWED_CHAT_SCOPES:
            await self.send(session, "REJECT chat bad scope")
            return
        if not text or len(text) > MAX_CHAT_LEN:
            await self.send(session, "REJECT chat bad length")
            return
        # ASCII sanitize: strip control chars (keep space + printable)
        text = "".join(c for c in text if c == " " or (ord(c) >= 32 and c != "\x7f"))
        if not text:
            return

        # Rate limit: token bucket. Refill at CHAT_RATE_PER_SEC tokens/sec, capped.
        now = asyncio.get_event_loop().time()
        dt = now - session.chat_last_refill
        session.chat_token_bucket = min(
            CHAT_RATE_PER_SEC,
            session.chat_token_bucket + dt * CHAT_RATE_PER_SEC,
        )
        session.chat_last_refill = now
        if session.chat_token_bucket < 1.0:
            log.info("chat rate-limited sid=%s", session.sid)
            return
        session.chat_token_bucket -= 1.0

        author = session.username or "anon"
        out = f"CHAT {scope} {author} {text}"

        if scope == "general":
            targets = [s for s in self.sessions.values() if s.state in ("logged_in", "queued", "matched")]
        elif scope == "match":
            if not session.match_id:
                await self.send(session, "REJECT chat no match")
                return
            m = self.matches.get(session.match_id)
            if not m:
                return
            targets = [self.sessions.get(m.sid_a), self.sessions.get(m.sid_b)]
            targets = [s for s in targets if s is not None]
        else:  # room
            if not session.room_code:
                await self.send(session, "REJECT chat no room")
                return
            room = self.rooms.get(session.room_code)
            if not room:
                return
            targets = [self.sessions.get(sid) for sid in room.members]
            targets = [s for s in targets if s is not None]

        for t in targets:
            try:
                await self.send(t, out)
            except Exception:
                pass
        log.info(
            "CHAT %s sid=%s -> %d targets: %s",
            scope, session.sid, len(targets), text[:64],
        )

    async def handle_room(self, session: ClientSession, payload: str):
        """ROOM CREATE <name> | JOIN <code> | LEAVE | LIST | SLOT A|B
           | UNSLOT | START | SETTINGS <key> <val>

        Rooms hold up to 8 members (host + slot players + spectators) and
        persist across matches so the post-match Soft_Reset_Sub → title
        attract path doesn't kick anyone out — the same room re-renders
        when the user navigates back to the Network menu."""
        if session.state not in ("logged_in", "queued", "matched"):
            await self.send(session, "REJECT not logged in")
            return
        parts = payload.split(" ", 2)
        sub = parts[0]

        if sub == "CREATE":
            name = (parts[1] if len(parts) > 1 else session.username)[:32]
            code = gen_room_code()
            while code in self.rooms:
                code = gen_room_code()
            self.rooms[code] = Room(code=code, name=name, host_sid=session.sid, members=[session.sid])
            session.room_code = code
            log.info("ROOM CREATE %s by %s (name=%r)", code, session.username, name)
            await self.send(session, f"ROOM CREATED {code} {name}")
            await self._broadcast_room_state(self.rooms[code])

        elif sub == "JOIN":
            code = (parts[1] if len(parts) > 1 else "").strip().upper()
            room = self.rooms.get(code)
            if not room:
                await self.send(session, "REJECT room not found")
                return
            if session.sid in room.members:
                return
            if len(room.members) >= ROOM_MAX_MEMBERS:
                await self.send(session, "REJECT room full")
                return
            room.members.append(session.sid)
            room.empty_since = None
            session.room_code = code
            log.info("ROOM JOIN %s by %s (count=%d)", code, session.username, len(room.members))
            await self.send(session, f"ROOM JOINED {code} {room.name}")
            await self._broadcast_room_state(room)

        elif sub == "LEAVE":
            for code, room in list(self.rooms.items()):
                if session.sid in room.members:
                    self._room_remove_member(room, session.sid)
                    session.room_code = None
                    await self.send(session, f"ROOM LEFT {code}")
                    log.info("ROOM LEAVE %s by %s", code, session.username)
                    if not room.members:
                        room.empty_since = time.time()
                    else:
                        await self._broadcast_room_state(room)
                    break

        elif sub == "LIST":
            for code, room in self.rooms.items():
                await self.send(session, f"ROOM INFO {code} {len(room.members)}/{ROOM_MAX_MEMBERS} {room.name}")
            await self.send(session, "ROOM LIST_END")

        elif sub == "SLOT":
            slot = (parts[1] if len(parts) > 1 else "").upper()
            if slot not in ("A", "B"):
                await self.send(session, "REJECT SLOT must be A or B")
                return
            room = self.rooms.get(session.room_code or "")
            if not room or session.sid not in room.members:
                await self.send(session, "REJECT not in a room")
                return
            # Clear any other slot occupancy by this sid first.
            if room.slot_a == session.sid: room.slot_a = None
            if room.slot_b == session.sid: room.slot_b = None
            # Then claim — only if target slot empty.
            if slot == "A" and room.slot_a is None:
                room.slot_a = session.sid
            elif slot == "B" and room.slot_b is None:
                room.slot_b = session.sid
            else:
                await self.send(session, "REJECT slot taken")
                return
            await self._broadcast_room_state(room)

        elif sub == "UNSLOT":
            room = self.rooms.get(session.room_code or "")
            if not room or session.sid not in room.members:
                return
            changed = False
            if room.slot_a == session.sid:
                room.slot_a = None
                changed = True
            if room.slot_b == session.sid:
                room.slot_b = None
                changed = True
            if changed:
                await self._broadcast_room_state(room)

        elif sub == "START":
            room = self.rooms.get(session.room_code or "")
            if not room:
                await self.send(session, "REJECT not in a room")
                return
            if session.sid != room.host_sid:
                await self.send(session, "REJECT only host can START")
                return
            if room.current_match_id is not None:
                await self.send(session, "REJECT match already in progress")
                return
            if not room.slot_a or not room.slot_b:
                await self.send(session, "REJECT need players in both slots")
                return
            a = self.sessions.get(room.slot_a)
            b = self.sessions.get(room.slot_b)
            if not a or not b:
                await self.send(session, "REJECT slot occupant disconnected")
                room.slot_a = None
                room.slot_b = None
                await self._broadcast_room_state(room)
                return
            await self._pair_room(room, a, b)

        elif sub == "SETTINGS":
            room = self.rooms.get(session.room_code or "")
            if not room:
                return
            if session.sid != room.host_sid:
                await self.send(session, "REJECT only host can change SETTINGS")
                return
            kv = (parts[1] if len(parts) > 1 else "").split(" ", 1)
            if len(kv) != 2:
                return
            key, val = kv[0], kv[1]
            try:
                if key == "best_of":
                    room.settings_best_of = max(1, min(9, int(val)))
                elif key == "timer":
                    room.settings_timer = max(15, min(99, int(val)))
                elif key == "damage":
                    room.settings_damage = max(0, min(2, int(val)))
                else:
                    return
            except ValueError:
                return
            await self._broadcast_room_state(room)

        else:
            await self.send(session, f"REJECT unknown ROOM subcommand: {sub}")

    def _room_remove_member(self, room: Room, sid: str):
        if sid in room.members:
            room.members.remove(sid)
        if room.slot_a == sid:
            room.slot_a = None
        if room.slot_b == sid:
            room.slot_b = None
        # Host migration: if host leaves, first remaining member takes over.
        if sid == room.host_sid and room.members:
            room.host_sid = room.members[0]

    async def _broadcast_room_state(self, room: Room):
        """Wire: ROOM STATE <code> host=<sid> best_of=<n> timer=<n> damage=<n>
                  slot_a=<user|-> slot_b=<user|-> match=<id|->
                  members=<user1,user2,...>

        Single-line snapshot of everything the room overlay needs. Sent to
        every current member on any change (member join/leave, slot pick,
        settings tweak, match start, match finalize)."""
        # Self-heal: if current_match_id references a match that no longer
        # exists (janitor reaped after no-RESULT abandonment) or has already
        # finalized, clear it so the host's Start button re-enables instead
        # of being permanently stuck at "Match in progress".
        if room.current_match_id is not None:
            m = self.matches.get(room.current_match_id)
            if m is None or m.finalized:
                room.current_match_id = None
        def uname(sid):
            s = self.sessions.get(sid) if sid else None
            return s.username if s else "-"
        members_str = ",".join(uname(sid) for sid in room.members) or "-"
        scores_str = ",".join(f"{u}:{w}" for u, w in room.scores.items()) or "-"
        msg = (f"ROOM STATE {room.code} "
               f"host={uname(room.host_sid)} "
               f"best_of={room.settings_best_of} "
               f"timer={room.settings_timer} "
               f"damage={room.settings_damage} "
               f"slot_a={uname(room.slot_a)} "
               f"slot_b={uname(room.slot_b)} "
               f"match={room.current_match_id or '-'} "
               f"scores={scores_str} "
               f"members={members_str}")
        for sid in list(room.members):
            s = self.sessions.get(sid)
            if s:
                await self.send(s, msg)

    async def _pair_room(self, room: Room, a: ClientSession, b: ClientSession):
        """Dispatch a match between slot_a/slot_b in a room. Room state
        PERSISTS across the match — slots stay, members stay, settings
        stay. _after_match_finalize re-broadcasts ROOM STATE with
        match=- so the room overlay knows it can START again."""
        # Clear any stale UDP endpoint snapshots from a prior match in this
        # room. handle_udp gates the START dispatch on both peers having
        # udp_endpoint set — if either side carried over its old endpoint
        # (NAT-allocated port that won't be valid for the new ephemeral UDP
        # socket the client recreates per match), the server would fire START
        # the moment the OTHER peer registered, before the user could even
        # see the Accept dialog, and the non-registered peer would land in
        # GAME_START without a live UDP socket → gekko stuck → blackscreen.
        a.udp_endpoint = None
        b.udp_endpoint = None
        match_id = str(uuid.uuid4())
        pm = PendingMatch(
            match_id=match_id, sid_a=a.sid, sid_b=b.sid,
            user_a=a.username, user_b=b.username,
        )
        # Stash room code on the match so _after_match_finalize can find the
        # room without scanning every entry in self.rooms.
        pm.room_code = room.code  # type: ignore[attr-defined]
        pm.platform_a = a.platform  # type: ignore[attr-defined]
        pm.platform_b = b.platform  # type: ignore[attr-defined]
        self.matches[match_id] = pm
        room.current_match_id = match_id
        try:
            self.db.execute(
                "INSERT INTO matches (id, p1, p2, created_at, region, platform_a, platform_b)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (match_id, a.username, b.username, int(time.time()),
                 self.region_code, a.platform or None, b.platform or None),
            )
            self.db.commit()
        except Exception as e:
            log.warning("match persist failed: %s", e)
        a.match_id = match_id
        b.match_id = match_id
        a.state = "matched"
        b.state = "matched"
        log.info("ROOM MATCH %s: %s vs %s (from room %s, members=%d)",
                 match_id, a.username, b.username, room.code, len(room.members))
        await self.send(a, f"MATCH {match_id} {b.username}")
        await self.send(b, f"MATCH {match_id} {a.username}")
        await self._broadcast_room_state(room)

    async def handle_result(self, session: ClientSession, payload: str):
        """RESULT <match_id> <my_wins> <opp_wins>"""
        parts = payload.split()
        if len(parts) != 3:
            log.warning("malformed RESULT sid=%s: %r", session.sid, payload)
            return
        match_id, my_wins_s, opp_wins_s = parts
        try:
            my_wins = int(my_wins_s)
            opp_wins = int(opp_wins_s)
        except ValueError:
            return
        # Sanity bounds (3rd Strike best-of-3 rounds normally, allow up to 9)
        if not (0 <= my_wins <= 9 and 0 <= opp_wins <= 9):
            log.warning("RESULT out of bounds sid=%s: %s/%s", session.sid, my_wins, opp_wins)
            return

        m = self.matches.get(match_id)
        if not m:
            return
        if m.finalized:
            # In-game rematch: the session keeps playing under the same
            # match_id and re-reports cumulative PL_Wins after every game.
            # A report extending the already-counted totals reopens the
            # match; the finalize path credits only the new game (delta).
            counted = getattr(m, "counted", None) or (0, 0)
            if (my_wins + opp_wins) <= (counted[0] + counted[1]):
                return  # duplicate/stale report of an already-counted game
            m.finalized = False
            m.result_a = None
            m.result_b = None
        if session.sid not in (m.sid_a, m.sid_b):
            log.warning("RESULT from wrong sid=%s for match=%s", session.sid, match_id)
            return

        if session.sid == m.sid_a:
            m.result_a = (my_wins, opp_wins)
        else:
            m.result_b = (my_wins, opp_wins)
        log.info("RESULT match=%s from sid=%s wins=%d/%d", match_id, session.sid, my_wins, opp_wins)

        # Wait for both
        if m.result_a is None or m.result_b is None:
            return

        # Validate concordance: A says (a_wins, b_wins), B says (b_wins, a_wins).
        a_wins_a, b_wins_a = m.result_a
        b_wins_b, a_wins_b = m.result_b
        if a_wins_a != a_wins_b or b_wins_a != b_wins_b:
            log.warning(
                "RESULT mismatch match=%s: A=%s/%s B=%s/%s (cheat attempt?)",
                match_id, a_wins_a, b_wins_a, a_wins_b, b_wins_b,
            )
            try:
                self.db.execute(
                    "UPDATE matches SET winner=?, completed_at=? WHERE id=?",
                    ("DISPUTED", int(time.time()), match_id),
                )
                self.db.commit()
            except Exception:
                pass
            m.finalized = True
            m.finalized_at = time.time()
            await self._after_match_finalize(m)
            return

        a_wins, b_wins = a_wins_a, b_wins_a
        # Per-game crediting for in-game rematches: only the delta vs the
        # already-counted totals decides this game's winner.
        prev_counted = getattr(m, "counted", None) or (0, 0)
        delta_a = a_wins - prev_counted[0]
        delta_b = b_wins - prev_counted[1]
        m.counted = (a_wins, b_wins)  # type: ignore[attr-defined]
        game_winner = None if delta_a == delta_b else (m.user_a if delta_a > delta_b else m.user_b)
        m.game_winner = game_winner  # type: ignore[attr-defined]
        if self.upstream_url:
            await self._upstream_post("/api/internal/result", {
                "match_id": match_id,
                "user_a": m.user_a,
                "user_b": m.user_b,
                "a_wins": a_wins,
                "b_wins": b_wins,
                "is_ranked": m.is_ranked,
                "region": self.region_code,
                "platform_a": getattr(m, "platform_a", "") or "",
                "platform_b": getattr(m, "platform_b", "") or "",
            })
            m.finalized = True
            m.finalized_at = time.time()
            await self._after_match_finalize(m)
            return
        if game_winner is None:
            # No new game to credit (tied first report, or stale duplicate).
            log.info("match %s ended as TIE", match_id)
            try:
                self.db.execute(
                    "UPDATE matches SET winner=?, completed_at=? WHERE id=?",
                    ("TIE", int(time.time()), match_id),
                )
                self.db.commit()
            except Exception:
                pass
            m.finalized = True
            m.finalized_at = time.time()
            await self._after_match_finalize(m)
            return

        winner = game_winner
        loser = m.user_b if winner == m.user_a else m.user_a
        if m.is_ranked:
            await self._apply_elo(match_id, winner, loser)
        else:
            # Casual: bump wins/losses (visible on leaderboard) but skip ELO.
            try:
                self.db.execute(
                    "UPDATE matches SET winner=?, completed_at=? WHERE id=?",
                    (winner, int(time.time()), match_id),
                )
                self.db.execute(
                    "UPDATE users SET wins=wins+1 WHERE username=?", (winner,),
                )
                self.db.execute(
                    "UPDATE users SET losses=losses+1 WHERE username=?", (loser,),
                )
                self.db.commit()
            except Exception:
                pass
            log.info("casual match %s done — %s beat %s (no ELO change)", match_id, winner, loser)
        m.finalized = True
        m.finalized_at = time.time()
        await self._after_match_finalize(m)

    async def _after_match_finalize(self, m: PendingMatch):
        """Hook after every finalize site. If the match belonged to a room,
        clear its current_match_id so the host can press START again, and
        re-broadcast ROOM STATE to all members so the overlay updates."""
        room_code = getattr(m, "room_code", None)
        if not room_code:
            return
        room = self.rooms.get(room_code)
        if not room:
            return
        if room.current_match_id == m.match_id:
            room.current_match_id = None
        # Multi-fight scoreboard: credit the winner of the game that just
        # finalized (delta-aware — in-game rematches re-finalize per game).
        gw = getattr(m, "game_winner", None)
        if gw:
            room.scores[gw] = room.scores.get(gw, 0) + 1
            m.game_winner = None  # consumed
        await self._broadcast_room_state(room)

    async def _apply_elo(self, match_id: str, winner: str, loser: str):
        """Standard ELO update with K=32."""
        K = 32
        try:
            row_w = self.db.execute("SELECT elo FROM users WHERE username=?", (winner,)).fetchone()
            row_l = self.db.execute("SELECT elo FROM users WHERE username=?", (loser,)).fetchone()
        except Exception as e:
            log.warning("elo fetch failed: %s", e)
            return
        if not row_w or not row_l:
            # Anon participant — skip ELO
            log.info("skip ELO match=%s (anon participant)", match_id)
            return
        ra, rb = row_w[0], row_l[0]
        ea = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
        eb = 1.0 - ea
        new_ra = ra + K * (1.0 - ea)
        new_rb = rb + K * (0.0 - eb)
        delta_w = int(round(new_ra - ra))
        delta_l = int(round(new_rb - rb))
        try:
            self.db.execute(
                "UPDATE users SET elo=elo+?, wins=wins+1 WHERE username=?",
                (delta_w, winner),
            )
            self.db.execute(
                "UPDATE users SET elo=MAX(0, elo+?), losses=losses+1 WHERE username=?",
                (delta_l, loser),
            )
            self.db.execute(
                "UPDATE matches SET winner=?, p1_elo_delta=?, p2_elo_delta=?, completed_at=? WHERE id=?",
                (
                    winner,
                    delta_w if winner == self.matches[match_id].user_a else delta_l,
                    delta_w if winner == self.matches[match_id].user_b else delta_l,
                    int(time.time()),
                    match_id,
                ),
            )
            self.db.commit()
        except Exception as e:
            log.warning("elo update failed: %s", e)
            return
        log.info(
            "ELO match=%s %s +%d (→%d) vs %s %d (→%d)",
            match_id, winner, delta_w, ra + delta_w, loser, delta_l, rb + delta_l,
        )

    async def _handle_internal_auth(self, body: dict) -> dict:
        """Upstream endpoint: register/login/refresh. Validates against local DB,
        issues HMAC token. Returns username + token + expiry."""
        action = body.get("action")
        version = (body.get("version") or "").strip()
        if version and version not in ALLOWED_VERSIONS:
            return {"ok": False, "reject": f"REJECT version mismatch — allowed: {','.join(sorted(ALLOWED_VERSIONS))}"}
        if action == "register":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            if not is_valid_username(username):
                return {"ok": False, "reject": "REJECT invalid username (3-16 chars, a-zA-Z0-9_)"}
            if len(password) < 6:
                return {"ok": False, "reject": "REJECT password too short (min 6)"}
            if self.db_get_user(username):
                return {"ok": False, "reject": "REJECT username taken"}
            try:
                self.db_create_user(username, password)
            except Exception as e:
                log.warning("internal register fail user=%s: %s", username, e)
                return {"ok": False, "reject": "REJECT register failed"}
            log.info("REGISTER (internal) user=%s ip=%s", username, body.get("ip"))
        elif action == "login":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            row = self.db_get_user(username)
            if not row or not verify_password(password, row[1]):
                return {"ok": False, "reject": "REJECT invalid username or password"}
            self.db_touch_login(username)
            log.info("LOGIN (internal) user=%s ip=%s elo=%d", username, body.get("ip"), row[2])
        elif action == "refresh":
            token = body.get("token") or ""
            username = verify_token(token)
            if not username:
                return {"ok": False, "reject": "REJECT invalid or expired token — please login again"}
            if not self.db_get_user(username):
                return {"ok": False, "reject": "REJECT user no longer exists"}
            self.db_touch_login(username)
            log.info("REFRESH (internal) user=%s ip=%s", username, body.get("ip"))
        else:
            return {"ok": False, "reject": "REJECT unknown action"}
        token, expiry = issue_token(username)
        return {
            "ok": True,
            "username": username,
            "display": username,
            "token": token,
            "expiry": expiry,
        }

    async def _handle_internal_result(self, body: dict) -> dict:
        """Upstream endpoint: finalize match recorded on edge server.
        Applies ELO + writes matches row. Idempotent on match_id."""
        match_id = body.get("match_id")
        user_a = body.get("user_a")
        user_b = body.get("user_b")
        a_wins = int(body.get("a_wins") or 0)
        b_wins = int(body.get("b_wins") or 0)
        is_ranked = bool(body.get("is_ranked"))
        if not match_id or not user_a or not user_b:
            return {"ok": False, "reject": "missing fields"}
        # Ensure matches row exists (edge server's match_id may not have been
        # inserted here). Insert lazily so completion writes succeed.
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO matches (id, p1, p2, created_at, region, platform_a, platform_b)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (match_id, user_a, user_b, int(time.time()),
                 body.get("region") or "unknown",
                 body.get("platform_a") or None,
                 body.get("platform_b") or None),
            )
            self.db.commit()
        except Exception as e:
            log.warning("internal result insert match failed: %s", e)
        # Stub a PendingMatch so _apply_elo can find it for row updates.
        # Mark started=True so janitor doesn't treat it as pre-punch stuck.
        from_existing = self.matches.get(match_id)
        if not from_existing:
            stub = PendingMatch(match_id=match_id, sid_a="", sid_b="",
                                user_a=user_a, user_b=user_b, is_ranked=is_ranked)
            stub.started = True
            self.matches[match_id] = stub
        m_stub = self.matches.get(match_id)
        prev_counted = (getattr(m_stub, "counted", None) or (0, 0)) if m_stub else (0, 0)
        delta_a = a_wins - prev_counted[0]
        delta_b = b_wins - prev_counted[1]
        if m_stub is not None:
            if (a_wins + b_wins) <= (prev_counted[0] + prev_counted[1]):
                return {"ok": True, "winner": "DUPLICATE"}
            m_stub.counted = (a_wins, b_wins)  # type: ignore[attr-defined]
        if delta_a == delta_b:
            try:
                self.db.execute(
                    "UPDATE matches SET winner=?, completed_at=? WHERE id=?",
                    ("TIE", int(time.time()), match_id),
                )
                self.db.commit()
            except Exception:
                pass
            log.info("(internal) match %s TIE", match_id)
            if m_stub:
                m_stub.finalized = True
                m_stub.finalized_at = time.time()
            return {"ok": True, "winner": "TIE"}
        winner = user_a if delta_a > delta_b else user_b
        loser = user_b if winner == user_a else user_a
        if is_ranked:
            await self._apply_elo(match_id, winner, loser)
        else:
            # For casual matches also bump wins/losses so leaderboard reflects
            # activity even when no ELO change.
            try:
                self.db.execute(
                    "UPDATE matches SET winner=?, completed_at=? WHERE id=?",
                    (winner, int(time.time()), match_id),
                )
                self.db.execute(
                    "UPDATE users SET wins=wins+1 WHERE username=?", (winner,),
                )
                self.db.execute(
                    "UPDATE users SET losses=losses+1 WHERE username=?", (loser,),
                )
                self.db.commit()
            except Exception:
                pass
            log.info("(internal) casual match %s done — %s beat %s", match_id, winner, loser)
        if m_stub:
            m_stub.finalized = True
            m_stub.finalized_at = time.time()
        return {"ok": True, "winner": winner}

    async def _upstream_post(self, path: str, body: dict) -> Optional[dict]:
        """POST JSON to upstream /api/internal/* endpoint. Returns parsed JSON or None on error."""
        if not self.upstream_url:
            return None
        import json as _json
        import urllib.request
        import urllib.error
        url = self.upstream_url + path
        data = _json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {INTERNAL_SECRET}",
            },
        )
        def _do():
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                try:
                    return e.read()
                except Exception:
                    return None
            except Exception as e:
                log.warning("upstream %s failed: %s", path, e)
                return None
        raw = await asyncio.to_thread(_do)
        if not raw:
            return None
        try:
            return _json.loads(raw.decode("utf-8"))
        except Exception as e:
            log.warning("upstream %s parse failed: %s", path, e)
            return None

    async def _proxy_auth(self, session: ClientSession, action: str, body: dict) -> bool:
        """Forward register/login/refresh to upstream. Returns True if session was logged in."""
        ip = session.peername[0] if session.peername else "?"
        body["action"] = action
        body["ip"] = ip
        resp = await self._upstream_post("/api/internal/auth", body)
        if resp is None:
            await self.send(session, "REJECT upstream unreachable — try again")
            return False
        if not resp.get("ok"):
            await self.send(session, resp.get("reject") or "REJECT auth failed")
            return False
        username = resp["username"]
        display = resp.get("display") or username
        token = resp["token"]
        expiry = resp["expiry"]
        session.username = username
        session.state = "logged_in"
        await self.send(session, f"TOKEN refresh {token} {expiry}")
        await self.send(session, f"PROFILE {display}")
        log.info("%s sid=%s ip=%s user=%s (via upstream)", action.upper(), session.sid, ip, username)
        await self._rebind_user_rooms(session)
        return True

    async def handle_register(self, session: ClientSession, payload: str):
        """REGISTER <username> <password> <version>"""
        ip = session.peername[0] if session.peername else "?"
        if not self._rate_check(ip, "hello", HELLO_RATE_LIMIT):
            self._register_reject(ip, "register rate")
            await self.send(session, "REJECT rate-limited (register)")
            return
        parts = payload.split(" ", 2)
        if len(parts) < 3:
            await self.send(session, "REJECT register requires: <username> <password> <version>")
            return
        username, password = parts[0], parts[1]
        version, _plat = _split_version_platform(parts[2])
        if _plat:
            session.platform = _plat
        if not self._check_version(session, version):
            await self.send(session, f"REJECT version mismatch — allowed: {','.join(sorted(ALLOWED_VERSIONS))}")
            return
        if self.upstream_url:
            await self._proxy_auth(session, "register",
                                   {"username": username, "password": password, "version": version})
            return
        if not is_valid_username(username):
            await self.send(session, "REJECT invalid username (3-16 chars, a-zA-Z0-9_)")
            return
        if len(password) < 6:
            await self.send(session, "REJECT password too short (min 6)")
            return
        if self.db_get_user(username):
            await self.send(session, "REJECT username taken")
            return
        try:
            self.db_create_user(username, password)
            log.info("REGISTER sid=%s ip=%s user=%s", session.sid, ip, username)
        except Exception as e:
            log.warning("register fail user=%s: %s", username, e)
            await self.send(session, "REJECT register failed")
            return
        await self._issue_session(session, username)

    async def handle_login(self, session: ClientSession, payload: str):
        """LOGIN <username> <password> <version>"""
        ip = session.peername[0] if session.peername else "?"
        if not self._rate_check(ip, "hello", HELLO_RATE_LIMIT):
            self._register_reject(ip, "login rate")
            await self.send(session, "REJECT rate-limited (login)")
            return
        parts = payload.split(" ", 2)
        if len(parts) < 3:
            await self.send(session, "REJECT login requires: <username> <password> <version>")
            return
        username, password = parts[0], parts[1]
        version, _plat = _split_version_platform(parts[2])
        if _plat:
            session.platform = _plat
        if not self._check_version(session, version):
            await self.send(session, f"REJECT version mismatch — allowed: {','.join(sorted(ALLOWED_VERSIONS))}")
            return
        if self.upstream_url:
            await self._proxy_auth(session, "login",
                                   {"username": username, "password": password, "version": version})
            return
        row = self.db_get_user(username)
        if not row or not verify_password(password, row[1]):
            self._register_reject(ip, "bad login")
            await self.send(session, "REJECT invalid username or password")
            return
        self.db_touch_login(username)
        log.info("LOGIN sid=%s ip=%s user=%s elo=%d", session.sid, ip, username, row[2])
        await self._issue_session(session, username)

    async def _issue_session(self, session: ClientSession, username: str):
        """Send TOKEN + PROFILE to a now-authenticated session."""
        session.username = username
        session.state = "logged_in"
        token, expiry = issue_token(username)
        await self.send(session, f"TOKEN refresh {token} {expiry}")
        # Full username — 1.7.28+ clients parse up to 63 chars; the room
        # overlay compares it against ROOM STATE names for host/slot detection.
        display = username
        await self.send(session, f"PROFILE {display}")
        await self._rebind_user_rooms(session)

    async def _rebind_user_rooms(self, session: ClientSession):
        """Reattach session to any room where this username is still listed
        under an older sid. Heals the post-match reconnect case: client TCP
        gets replaced (game returns to Network menu, region switch, brief
        network blip), a fresh sid is allocated, the user re-authenticates,
        but the room's member/host/slot entries still point at the dead sid
        — so ROOM START etc. comes back as 'not in a room'. We swap the
        membership over to the new sid and rebroadcast STATE so the client
        observes a consistent room view immediately.
        """
        new_sid = session.sid
        username = session.username
        if not username:
            return
        for room in list(self.rooms.values()):
            old_sid = None
            for sid in room.members:
                if sid == new_sid:
                    continue
                prior = self.sessions.get(sid)
                if prior is not None and prior.username == username:
                    old_sid = sid
                    break
            if old_sid is None:
                continue
            room.members = [new_sid if sid == old_sid else sid for sid in room.members]
            if room.slot_a == old_sid:
                room.slot_a = new_sid
            if room.slot_b == old_sid:
                room.slot_b = new_sid
            if room.host_sid == old_sid:
                room.host_sid = new_sid
            room.empty_since = None
            # Old session may still be open (TCP reset isn't always observable
            # immediately). Close it so it can't race with the new session.
            old = self.sessions.pop(old_sid, None)
            if old is not None:
                try:
                    old.writer.close()
                except Exception:
                    pass
            log.info("room %s: rebind %s -> %s (user=%s)",
                     room.code, old_sid, new_sid, username)
            await self._broadcast_room_state(room)

    # ---------- Auth (stubbed) ----------

    def _check_version(self, session: ClientSession, version: str) -> bool:
        """Returns True if version allowed, else REJECTs + closes connection."""
        ip = session.peername[0] if session.peername else "?"
        if not version:
            log.warning("missing version sid=%s ip=%s", session.sid, ip)
            self._register_reject(ip, "missing version")
            return False
        if version not in ALLOWED_VERSIONS:
            log.warning("VERSION MISMATCH sid=%s ip=%s version=%s allowed=%s",
                        session.sid, ip, version, ALLOWED_VERSIONS)
            self._register_reject(ip, f"version {version}")
            return False
        session.version = version
        return True

    async def handle_hello(self, session: ClientSession, version: str):
        """Anonymous HELLO — only allowed when ALLOW_ANON. Otherwise must REGISTER/LOGIN."""
        version, _plat = _split_version_platform(version)
        if _plat:
            session.platform = _plat
        ip = session.peername[0] if session.peername else "?"
        if not self._rate_check(ip, "hello", HELLO_RATE_LIMIT):
            self._register_reject(ip, "hello rate")
            await self.send(session, "REJECT rate-limited (hello)")
            return
        if not self._check_version(session, version):
            await self.send(session, f"REJECT version mismatch — allowed: {','.join(sorted(ALLOWED_VERSIONS))}")
            return
        if not ALLOW_ANON:
            await self.send(session, "REJECT anonymous login disabled — use REGISTER or LOGIN")
            return
        # Allocate ephemeral anon username (not stored in DB)
        anon = "anon" + secrets.token_hex(2)[:3]
        await self.complete_login_anon(session, anon)

    async def complete_login_anon(self, session: ClientSession, username: str):
        session.username = username
        session.state = "logged_in"
        # Short-lived dummy token (won't survive REFRESH check)
        expiry = int(time.time()) + 24 * 3600
        await self.send(session, f"TOKEN refresh anon.{expiry}.x {expiry}")
        await self.send(session, f"PROFILE {username[:7]}")

    async def handle_refresh(self, session: ClientSession, token: str, version: str):
        version, _plat = _split_version_platform(version)
        if _plat:
            session.platform = _plat
        ip = session.peername[0] if session.peername else "?"
        if not self._rate_check(ip, "hello", HELLO_RATE_LIMIT):
            self._register_reject(ip, "refresh rate")
            await self.send(session, "REJECT rate-limited (refresh)")
            return
        if not self._check_version(session, version):
            await self.send(session, f"REJECT version mismatch — allowed: {','.join(sorted(ALLOWED_VERSIONS))}")
            return
        if self.upstream_url:
            await self._proxy_auth(session, "refresh",
                                   {"token": token, "version": version})
            return
        username = verify_token(token)
        if not username:
            await self.send(session, "REJECT invalid or expired token — please login again")
            return
        if not self.db_get_user(username):
            await self.send(session, "REJECT user no longer exists")
            return
        self.db_touch_login(username)
        log.info("REFRESH sid=%s ip=%s user=%s", session.sid, ip, username)
        await self._issue_session(session, username)

    # complete_login replaced by _issue_session (DB-backed) and complete_login_anon

    # ---------- Queue / match ----------

    async def handle_queue_add(self, session: ClientSession, mode: str):
        ip = session.peername[0] if session.peername else "?"
        if not self._rate_check(ip, "queue", QUEUE_RATE_LIMIT):
            self._register_reject(ip, "queue rate")
            await self.send(session, "REJECT rate-limited (queue)")
            return
        if session.state != "logged_in":
            log.warning("queue add from non-logged-in sid=%s state=%s", session.sid, session.state)
            return
        # Anonymous users can only play casual
        is_anon = session.username.startswith("anon")
        if mode == "ranked" and is_anon:
            await self.send(session, "REJECT ranked requires registered account")
            return
        # Remove from other queue if present
        if session.sid in self.queue_casual:
            self.queue_casual.remove(session.sid)
        if session.sid in self.queue_ranked:
            self.queue_ranked.remove(session.sid)
        queue = self.queue_ranked if mode == "ranked" else self.queue_casual
        queue.append(session.sid)
        session.state = "queued"
        session.queued_at = time.time()
        session.queue_mode = mode
        log.info("queue add sid=%s mode=%s ip=%s ver=%s (sizes casual=%d ranked=%d)",
                 session.sid, mode, ip, session.version,
                 len(self.queue_casual), len(self.queue_ranked))
        await self.try_match()

    async def handle_queue_remove(self, session: ClientSession):
        for q in (self.queue_casual, self.queue_ranked):
            if session.sid in q:
                q.remove(session.sid)
        session.state = "logged_in"
        log.info("queue remove sid=%s", session.sid)

    async def try_match(self):
        # Try ranked first, then casual
        for queue, is_ranked in [(self.queue_ranked, True), (self.queue_casual, False)]:
            while len(queue) >= 2:
                sid_a = queue.pop(0)
                sid_b = queue.pop(0)
                a = self.sessions.get(sid_a)
                b = self.sessions.get(sid_b)
                if not a or not b:
                    continue
                # Wipe any prior UDP endpoint so handle_udp doesn't fire START
                # using a stale port from a previous queue match. See _pair_room
                # for the full rationale.
                a.udp_endpoint = None
                b.udp_endpoint = None
                match_id = str(uuid.uuid4())
                pm = PendingMatch(
                    match_id=match_id, sid_a=sid_a, sid_b=sid_b,
                    user_a=a.username, user_b=b.username,
                    is_ranked=is_ranked,
                )
                pm.platform_a = a.platform  # type: ignore[attr-defined]
                pm.platform_b = b.platform  # type: ignore[attr-defined]
                self.matches[match_id] = pm
                try:
                    self.db.execute(
                        "INSERT INTO matches (id, p1, p2, created_at, region, platform_a, platform_b)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (match_id, a.username, b.username, int(time.time()),
                         self.region_code, a.platform or None, b.platform or None),
                    )
                    self.db.commit()
                except Exception as e:
                    log.warning("match persist failed: %s", e)
                a.match_id = match_id
                b.match_id = match_id
                a.state = "matched"
                b.state = "matched"
                mode_label = "ranked" if is_ranked else "casual"
                log.info("MATCH %s [%s]: %s vs %s", match_id, mode_label, a.username, b.username)
                await self.send(a, f"MATCH {match_id} {b.username}")
                await self.send(b, f"MATCH {match_id} {a.username}")

    async def handle_decline(self, session: ClientSession, match_id: str):
        m = self.matches.get(match_id)
        if not m:
            return
        del self.matches[match_id]
        self.relay_routes.pop(match_id, None)
        for sid in (m.sid_a, m.sid_b):
            s = self.sessions.get(sid)
            if s and s.sid != session.sid:
                await self.send(s, f"CANCEL {match_id}")
                s.state = "logged_in"
                s.match_id = None
        session.state = "logged_in"
        session.match_id = None
        log.info("DECLINE %s by sid=%s", match_id, session.sid)

    # ---------- UDP handler (NAT punch + START dispatch) ----------

    async def handle_udp(self, data: bytes, addr: tuple):
        try:
            payload = data.decode("utf-8", errors="ignore").strip()
        except Exception:
            return
        # payload format: "<session_id> <match_id>"
        parts = payload.split(" ", 1)
        if len(parts) != 2:
            log.warning("malformed UDP from %s: %r", addr, payload)
            return
        sid, match_id = parts[0], parts[1]
        session = self.sessions.get(sid)
        if not session:
            log.warning("UDP unknown sid=%s from %s", sid, addr)
            return
        if session.match_id != match_id:
            log.warning("UDP wrong match_id sid=%s", sid)
            return

        # Record endpoint (first time or refresh)
        first_time = session.udp_endpoint is None
        session.udp_endpoint = addr
        if first_time:
            log.info("UDP register sid=%s @ %s for match %s", sid, addr, match_id)
            await self.send(session, "UDP ok")

        # Check if both peers have registered → send START
        m = self.matches.get(match_id)
        if not m:
            return
        a = self.sessions.get(m.sid_a)
        b = self.sessions.get(m.sid_b)
        if not a or not b:
            return
        if a.udp_endpoint and b.udp_endpoint:
            # Send START to each with peer endpoint, then mark dispatched
            if m.match_id in self.matches:
                relay_endpoint = f"{self.public_host}:{self.relay_port}"
                # Heuristic: if both peers reported the same external NAT'd IP
                # they're behind the same router. Hairpin NAT is unreliable on
                # most consumer routers -> force relay. Different IPs -> try
                # direct P2P; relay endpoint still shipped as fallback.
                same_external_ip = a.udp_endpoint[0] == b.udp_endpoint[0]
                # Either client opting out of P2P forces relay for both.
                force_relay_peer = a.force_relay or b.force_relay
                # LAN-direct fast-path: same external NAT + both clients sent
                # UDP_LAN → swap direct endpoint to peer's LAN address. Avoids
                # the hairpin-NAT pitfall (most consumer routers won't reflect
                # a public address back to the same public address) and saves
                # the 2× server hop (~120ms via sa-east-1 from Chile → ~5ms
                # LAN-direct).
                # LAN-direct fast-path: find a (a_ip, b_ip) pair from the two
                # peers' advertised candidate lists where both share a /24
                # prefix. That filters out Hyper-V / WSL2 virtual interfaces
                # (peers won't share those CIDRs) and APIPA (already rejected
                # client-side). Port reused from NAT-punched udp_endpoint
                # (port-preserving NAT assumption).
                a_lan = None
                b_lan = None
                if same_external_ip and not force_relay_peer and a.lan_ips and b.lan_ips:
                    for ai in a.lan_ips:
                        a_prefix = ".".join(ai.split(".")[:3])
                        for bi in b.lan_ips:
                            if bi.startswith(a_prefix + "."):
                                a_lan, b_lan = ai, bi
                                break
                        if a_lan:
                            break
                lan_direct = a_lan is not None and b_lan is not None
                if lan_direct:
                    use_relay = 0
                    a_direct = f"{b_lan}:{b.udp_endpoint[1]}"
                    b_direct = f"{a_lan}:{a.udp_endpoint[1]}"
                else:
                    use_relay = 1 if (same_external_ip or force_relay_peer) else 0
                    a_direct = f"{b.udp_endpoint[0]}:{b.udp_endpoint[1]}"
                    b_direct = f"{a.udp_endpoint[0]}:{a.udp_endpoint[1]}"
                # Wire: START <player> <relay_endpoint> <uuid> <use_relay> <peer_direct>
                await self.send(a, f"START 1 {relay_endpoint} {match_id} {use_relay} {a_direct}")
                await self.send(b, f"START 2 {relay_endpoint} {match_id} {use_relay} {b_direct}")
                self.relay_routes[match_id] = {"1": None, "2": None}
                m.started = True
                log.info("START dispatched for match %s (use_relay=%d, same_ip=%s, lan_direct=%s)",
                         match_id, use_relay, same_external_ip, lan_direct)

    async def handle_relay(self, data: bytes, addr: tuple) -> None:
        # Wire format: 36 chars uuid + 1 char side + payload.
        if len(data) < 37:
            return
        uuid = data[:36].decode("ascii", errors="ignore")
        side = chr(data[36])
        if side not in ("1", "2"):
            return
        route = self.relay_routes.get(uuid)
        if route is None:
            return  # no such match
        # Lazy registration: record sender's relay endpoint on first packet.
        route[side] = addr
        # Refresh finalized_at on activity so REMATCH keeps the relay alive.
        m = self.matches.get(uuid)
        if m is not None and m.finalized:
            m.finalized_at = time.time()
        # Forward to the other side, if known.
        other_side = "2" if side == "1" else "1"
        dst = route.get(other_side)
        if dst is None:
            return  # warm-up: peer hasn't sent yet
        # Rewrite the side byte so recipient sees their own side.
        payload = bytearray(data)
        payload[36] = ord(other_side)
        if self.relay_transport is not None:
            try:
                self.relay_transport.sendto(bytes(payload), dst)
            except Exception as e:
                log.warning("relay sendto %s failed: %s", dst, e)

    # ---------- Cleanup ----------

    async def cleanup_session(self, sid: str):
        session = self.sessions.pop(sid, None)
        if not session:
            return
        for q in (self.queue_casual, self.queue_ranked):
            if sid in q:
                q.remove(sid)
        # Clean rooms — disconnect = soft-leave. Room stays alive (grace
        # window) so the user can soft-reset / reconnect and rejoin the
        # same code. Janitor reaps after ROOM_EMPTY_TIMEOUT_S of no members.
        for code, room in list(self.rooms.items()):
            if sid in room.members:
                self._room_remove_member(room, sid)
                if not room.members:
                    room.empty_since = time.time()
                else:
                    # Best-effort broadcast new state; if writers are gone we
                    # don't care, members list now reflects reality.
                    try:
                        await self._broadcast_room_state(room)
                    except Exception:
                        pass
        # Cancel any pending match involving this sid (skip finalized — those
        # already have a recorded winner; the disconnect is just end-of-match).
        for mid, m in list(self.matches.items()):
            if sid in (m.sid_a, m.sid_b):
                if m.finalized:
                    continue
                other_sid = m.sid_b if sid == m.sid_a else m.sid_a
                other = self.sessions.get(other_sid)
                if other:
                    await self.send(other, f"CANCEL {mid}")
                    other.state = "logged_in"
                    other.match_id = None
                del self.matches[mid]
        if session and session.match_id:
            self.relay_routes.pop(session.match_id, None)
        try:
            session.writer.close()
            await session.writer.wait_closed()
        except Exception:
            pass

    # ---------- Janitor ----------

    async def janitor_loop(self):
        """Periodic cleanup: idle queue + stuck matches."""
        while True:
            await asyncio.sleep(10)
            now = time.time()
            # Kick idle queued clients
            for q in (self.queue_casual, self.queue_ranked):
                for sid in list(q):
                    s = self.sessions.get(sid)
                    if s and (now - s.queued_at) > QUEUE_IDLE_TIMEOUT_S:
                        log.info("janitor: kick idle sid=%s", sid)
                        q.remove(sid)
                        s.state = "logged_in"
            # Reap rooms that have been empty (no members) past grace.
            for code, room in list(self.rooms.items()):
                if not room.members and room.empty_since is not None:
                    if (now - room.empty_since) > ROOM_EMPTY_TIMEOUT_S:
                        log.info("janitor: reap empty room %s", code)
                        del self.rooms[code]
            # If any room references a current_match_id that's already
            # finalized OR no longer in self.matches, clear + rebroadcast so
            # the Start button re-enables. Cheaper than a per-match scan
            # because rooms is usually tiny.
            for room in list(self.rooms.values()):
                if room.current_match_id is None:
                    continue
                m = self.matches.get(room.current_match_id)
                if m is None or m.finalized:
                    room.current_match_id = None
                    try:
                        await self._broadcast_room_state(room)
                    except Exception:
                        pass

            # Cancel stuck matches (no punch within timeout)
            for mid, m in list(self.matches.items()):
                age = now - m.created_at
                # Finalized matches: linger briefly so late duplicate RESULTs
                # find them (and clients see consistent state), then drop.
                if m.finalized:
                    finalize_age = now - m.finalized_at if m.finalized_at > 0 else age
                    if finalize_age > FINALIZE_GRACE_S:
                        del self.matches[mid]
                        self.relay_routes.pop(mid, None)
                    continue
                # In-progress matches that have started gameplay: keep for
                # match duration (max 30 min). Older = abandoned, drop silent.
                if m.started:
                    if age > 30 * 60:
                        log.info("janitor: drop abandoned in-progress match %s (age=%.0fs)", mid, age)
                        del self.matches[mid]
                        self.relay_routes.pop(mid, None)
                    continue
                # Pre-gameplay: someone didn't accept (UDP-register) in time.
                # Identify non-responder(s) and punish only them — the player
                # who accepted should not lose their queue spot.
                if age > MATCH_PUNCH_TIMEOUT_S:
                    a = self.sessions.get(m.sid_a)
                    b = self.sessions.get(m.sid_b)
                    a_accepted = a is not None and a.udp_endpoint is not None
                    b_accepted = b is not None and b.udp_endpoint is not None
                    for sid, sess, accepted in ((m.sid_a, a, a_accepted),
                                                (m.sid_b, b, b_accepted)):
                        if sess is None:
                            continue
                        await self.send(sess, f"CANCEL {mid}")
                        sess.match_id = None
                        sess.udp_endpoint = None
                        if accepted and (a_accepted != b_accepted):
                            # Partial accept: this player accepted, the other
                            # didn't. Re-queue them to their previous mode.
                            mode = sess.queue_mode or "casual"
                            queue = self.queue_ranked if mode == "ranked" else self.queue_casual
                            if sess.sid not in queue:
                                queue.append(sess.sid)
                            sess.state = "queued"
                            sess.queued_at = now
                        else:
                            # No-accept (both timed out, or this is the one who
                            # didn't respond): kick to logged_in. No re-queue.
                            sess.state = "logged_in"
                    log.info(
                        "janitor: accept-timeout match %s (a_accepted=%s b_accepted=%s)",
                        mid, a_accepted, b_accepted,
                    )
                    del self.matches[mid]
                    self.relay_routes.pop(mid, None)

    # ---------- Stats HTTP ----------

    def _build_stats_dict(self):
        import json as _json
        try:
            cur = self.db.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]
            # Sort: ranked players first by ELO desc, then unranked (less than PLACEMENT) by wins
            cur = self.db.execute(
                "SELECT username, elo, wins, losses, last_login_at FROM users "
                "ORDER BY (wins + losses >= ?) DESC, elo DESC LIMIT 50",
                (PLACEMENT_MATCHES,),
            )
            leaderboard = []
            for r in cur.fetchall():
                total = r[2] + r[3]
                rank = sf6_rank(r[1], total)
                leaderboard.append({
                    "username": r[0],
                    "elo": r[1] if total >= PLACEMENT_MATCHES else None,
                    "wins": r[2],
                    "losses": r[3],
                    "last_login_at": r[4],
                    "rank": rank["label"],
                    "tier": rank["tier"],
                    "sub": rank["sub"],
                })
            # Platforms + regions per player, from completed matches (1.7.28).
            _PLAT_LABEL = {"windows": "PC", "macos": "PC", "linux": "PC",
                           "android": "Android", "vita": "Vita"}
            plat_map: dict = {}
            reg_map: dict = {}
            cur = self.db.execute(
                "SELECT p1, platform_a, p2, platform_b, region FROM matches"
                " WHERE completed_at IS NOT NULL")
            for _p1, _pa, _p2, _pb, _reg in cur.fetchall():
                if _pa:
                    plat_map.setdefault(_p1, set()).add(_PLAT_LABEL.get(_pa, _pa))
                if _pb:
                    plat_map.setdefault(_p2, set()).add(_PLAT_LABEL.get(_pb, _pb))
                if _reg and _reg != "unknown":
                    reg_map.setdefault(_p1, set()).add(_reg)
                    reg_map.setdefault(_p2, set()).add(_reg)
            for entry in leaderboard:
                entry["platforms"] = sorted(plat_map.get(entry["username"], ()))
                entry["regions"] = sorted(reg_map.get(entry["username"], ()))
        except Exception as e:
            log.warning("leaderboard query failed: %s", e)
            total_users = 0
            leaderboard = []
        return {
            "connected": len(self.sessions),
            "queue": len(self.queue_casual) + len(self.queue_ranked),
            "queue_casual": len(self.queue_casual),
            "queue_ranked": len(self.queue_ranked),
            "matches_pending": len(self.matches),
            "users_connected": [s.username for s in self.sessions.values() if s.username],
            "users_total": total_users,
            "leaderboard": leaderboard,
            "versions": list({s.version for s in self.sessions.values() if s.version}),
            "bans_count": len(self.bans),
            "allowed_versions": sorted(ALLOWED_VERSIONS),
            "allow_anon": ALLOW_ANON,
        }

    def _render_leaderboard_html(self, stats: dict) -> str:
        import html as _html
        rows = []
        for i, p in enumerate(stats["leaderboard"], 1):
            color = TIER_COLOR.get(p["tier"], "#c9d1d9")
            total = p["wins"] + p["losses"]
            wr = (p["wins"] * 100 // total) if total else 0
            elo_disp = str(p["elo"]) if p["elo"] is not None else "—"
            safe_name = _html.escape(p["username"])
            rows.append(
                f'<tr><td class="rk">#{i}</td>'
                f'<td class="usr"><a href="/user/{safe_name}">{safe_name}</a></td>'
                f'<td class="rnk" style="color:{color}">{_html.escape(p["rank"])}</td>'
                f'<td class="elo">{elo_disp}</td>'
                f'<td class="wl">{p["wins"]}W / {p["losses"]}L</td>'
                f'<td class="wr">{wr}%</td>'
                f'<td class="plat">{_html.escape(" / ".join(p.get("platforms", [])) or "—")}</td>'
                f'<td class="reg">{_html.escape(" / ".join(p.get("regions", [])) or "—")}</td></tr>'
            )
        rows_html = "\n".join(rows) if rows else (
            '<tr><td colspan="8" class="empty">No matches played yet — be the first!</td></tr>'
        )
        return BASE_HTML.format(
            title="3SX Netplay — Rollback online for Street Fighter III",
            content=f"""
<section class="hero">
  <h1>
    <span data-i18n-en>Street Fighter III online,<br><span class="accent">the way it should be.</span></span>
    <span data-i18n-es>Street Fighter III online,<br><span class="accent">como debe ser.</span></span>
  </h1>
  <p class="tagline">
    <span data-i18n-en>Rollback netcode. Global matchmaking. SF6-style ranks. Built by the community.</span>
    <span data-i18n-es>Rollback netcode. Matchmaking global. Rangos estilo SF6. Hecho por la comunidad.</span>
  </p>
</section>

<h2>
  <span data-i18n-en>Server Live</span>
  <span data-i18n-es>Servidor en Vivo</span>
</h2>
<div class="stats-row">
  <div class="stat"><div class="val">{stats['connected']}</div>
    <div class="lbl"><span data-i18n-en>Online</span><span data-i18n-es>Conectados</span></div></div>
  <div class="stat"><div class="val">{stats['queue']}</div>
    <div class="lbl"><span data-i18n-en>In Queue</span><span data-i18n-es>En Cola</span></div></div>
  <div class="stat"><div class="val">{stats['matches_pending']}</div>
    <div class="lbl"><span data-i18n-en>Active Matches</span><span data-i18n-es>Peleas Activas</span></div></div>
  <div class="stat"><div class="val">{stats['users_total']}</div>
    <div class="lbl"><span data-i18n-en>Registered Players</span><span data-i18n-es>Jugadores Registrados</span></div></div>
</div>

<h2>
  <span data-i18n-en>Leaderboard</span>
  <span data-i18n-es>Tabla de Clasificación</span>
</h2>
<p class="sub">
  <span data-i18n-en>Players are unranked until they finish {PLACEMENT_MATCHES} placement matches. ELO ±32 per game (K=32).</span>
  <span data-i18n-es>Los jugadores quedan Unranked hasta completar {PLACEMENT_MATCHES} partidas de clasificación. ±32 ELO por partida (K=32).</span>
</p>
<table>
<thead><tr>
  <th>#</th>
  <th><span data-i18n-en>Player</span><span data-i18n-es>Jugador</span></th>
  <th><span data-i18n-en>Rank</span><span data-i18n-es>Rango</span></th>
  <th>ELO</th>
  <th><span data-i18n-en>Record</span><span data-i18n-es>Récord</span></th>
  <th><span data-i18n-en>WR</span><span data-i18n-es>WR</span></th>
  <th><span data-i18n-en>Platforms</span><span data-i18n-es>Plataformas</span></th>
  <th><span data-i18n-en>Regions</span><span data-i18n-es>Regiones</span></th>
</tr></thead>
<tbody>{rows_html}</tbody></table>

<h2>
  <span data-i18n-en>How to Play</span>
  <span data-i18n-es>Cómo Jugar</span>
</h2>
<ol class="steps">
  <li><span data-i18n-en>Download the latest build (Windows 10/11 x64) — see the GitHub release.</span>
      <span data-i18n-es>Descarga la última build (Windows 10/11 x64) — ver release en GitHub.</span></li>
  <li><span data-i18n-en>You need your own legally-dumped Street Fighter III: 3rd Strike PS2 ISO (NTSC-J). The game asks for it on first launch.</span>
      <span data-i18n-es>Necesitas tu propia ISO de Street Fighter III: 3rd Strike PS2 (NTSC-J) dumpeada legalmente. El juego la pide al primer arranque.</span></li>
  <li><span data-i18n-en>Run <code>3sx_launcher_online.exe</code> — register a username and password.</span>
      <span data-i18n-es>Ejecuta <code>3sx_launcher_online.exe</code> — registra un usuario y contraseña.</span></li>
  <li><span data-i18n-en>Click LAUNCH GAME — matchmaking finds you an opponent. Or create a private room and share the code with a friend.</span>
      <span data-i18n-es>Click LAUNCH GAME — el matchmaking te encuentra rival. O crea una sala privada y comparte el código con un amigo.</span></li>
  <li><span data-i18n-en>Win 5 placement matches to reveal your rank. Climb the leaderboard.</span>
      <span data-i18n-es>Gana 5 partidas de clasificación para revelar tu rango. Sube en la tabla.</span></li>
</ol>

<h2>
  <span data-i18n-en>Features</span>
  <span data-i18n-es>Características</span>
</h2>
<div class="cards">
  <div class="card"><span class="icon"></span>
    <h3><span data-i18n-en>True Rollback</span><span data-i18n-es>Rollback Real</span></h3>
    <p><span data-i18n-en>GekkoNet rollback netcode. No input delay. Compensates lag automatically.</span>
       <span data-i18n-es>Rollback netcode con GekkoNet. Sin input delay. Compensa lag automático.</span></p>
  </div>
  <div class="card"><span class="icon"></span>
    <h3><span data-i18n-en>SF6-Style Ranks</span><span data-i18n-es>Rangos Estilo SF6</span></h3>
    <p><span data-i18n-en>Rookie, Iron, Bronze, Silver, Gold, Platinum, Diamond, Master, Legend. Five sub-tiers each.</span>
       <span data-i18n-es>Rookie, Iron, Bronze, Silver, Gold, Platinum, Diamond, Master, Legend. Cinco sub-niveles cada uno.</span></p>
  </div>
  <div class="card"><span class="icon"></span>
    <h3><span data-i18n-en>Private Rooms</span><span data-i18n-es>Salas Privadas</span></h3>
    <p><span data-i18n-en>Generate a 6-character code, share it, and play with friends only.</span>
       <span data-i18n-es>Genera un código de 6 caracteres, compártelo, y juega solo con amigos.</span></p>
  </div>
  <div class="card"><span class="icon"></span>
    <h3><span data-i18n-en>Global Chat</span><span data-i18n-es>Chat Global</span></h3>
    <p><span data-i18n-en>Talk to other players in the launcher before and after matches.</span>
       <span data-i18n-es>Habla con otros jugadores en el launcher antes y después de las peleas.</span></p>
  </div>
  <div class="card"><span class="icon"></span>
    <h3><span data-i18n-en>Anti-Cheat</span><span data-i18n-es>Anti-Trampa</span></h3>
    <p><span data-i18n-en>Version handshake, rate limits, desync detection. Disputed matches don't count.</span>
       <span data-i18n-es>Verificación de versión, rate limits, detección de desync. Partidas en disputa no cuentan.</span></p>
  </div>
  <div class="card"><span class="icon"></span>
    <h3><span data-i18n-en>Match History</span><span data-i18n-es>Historial de Partidas</span></h3>
    <p><span data-i18n-en>Every match recorded. Click any player to view their record.</span>
       <span data-i18n-es>Cada partida queda registrada. Click en cualquier jugador para ver su historial.</span></p>
  </div>
</div>

<div class="footer">
  <p><span data-i18n-en>Open source · </span><span data-i18n-es>Código abierto · </span>
     <a href="https://github.com/SalieriMZ/3sx-online">GitHub</a> ·
     <a href="https://discord.gg/aume4RqnnP">Discord</a> ·
     <a href="/watch"><span data-i18n-en>Live Matches</span><span data-i18n-es>Peleas en Vivo</span></a> ·
     <a href="/api">JSON API</a> ·
     <a href="/metrics">Metrics</a></p>
  <p style="margin-top:.5rem;font-size:.8rem">
    <span data-i18n-en>Independent community project — not affiliated with Capcom or crowded-street. Based on </span>
    <span data-i18n-es>Proyecto comunitario independiente — sin afiliación con Capcom ni crowded-street. Basado en </span>
    <a href="https://github.com/crowded-street/3sx">crowded-street/3sx</a>.
    <span data-i18n-en>Game assets © Capcom. ROM required (not distributed).</span>
    <span data-i18n-es>Assets del juego © Capcom. ROM requerida (no se distribuye).</span>
  </p>
</div>
""",
        )

    def _render_watch_list(self) -> str:
        """List active matches with metadata."""
        import html as _html
        rows = []
        now = time.time()
        for mid, m in self.matches.items():
            elapsed = int(now - m.created_at)
            mins, secs = divmod(elapsed, 60)
            status_en = "In progress" if m.result_a or m.result_b else "Waiting"
            status_es = "En curso" if m.result_a or m.result_b else "Esperando"
            short_id = mid.split("-")[0]
            rows.append(
                f'<tr><td><a href="/watch/{mid}"><code>{short_id}</code></a></td>'
                f'<td>{_html.escape(m.user_a)} <span style="color:#8b949e">vs</span> {_html.escape(m.user_b)}</td>'
                f'<td>{mins:02d}:{secs:02d}</td>'
                f'<td><span data-i18n-en>{status_en}</span><span data-i18n-es>{status_es}</span></td></tr>'
            )
        rows_html = "\n".join(rows) if rows else (
            '<tr><td colspan="4" class="empty"><span data-i18n-en>No active matches right now.</span>'
            '<span data-i18n-es>No hay peleas activas en este momento.</span></td></tr>'
        )
        return BASE_HTML.format(
            title="Watch — 3SX Netplay",
            content=f"""
<p class="back"><a href="/">← <span data-i18n-en>Back</span><span data-i18n-es>Volver</span></a></p>
<h1><span data-i18n-en>Live Matches</span><span data-i18n-es>Peleas en Vivo</span></h1>
<p class="sub">
  <span data-i18n-en>Currently active matches on the server. Click a match for live status. Auto-refresh every 5s.</span>
  <span data-i18n-es>Peleas activas en el servidor. Click en una pelea para ver estado en vivo. Auto-refresca cada 5s.</span>
</p>
<table>
<thead><tr>
  <th>ID</th>
  <th><span data-i18n-en>Players</span><span data-i18n-es>Jugadores</span></th>
  <th><span data-i18n-en>Duration</span><span data-i18n-es>Duración</span></th>
  <th><span data-i18n-en>Status</span><span data-i18n-es>Estado</span></th>
</tr></thead>
<tbody>{rows_html}</tbody></table>
<script>setTimeout(function(){{location.reload();}}, 5000);</script>
""",
        )

    def _render_watch_match(self, match_id: str) -> tuple[str, int]:
        import html as _html
        if not re.match(r"^[a-f0-9-]{8,40}$", match_id):
            return (BASE_HTML.format(
                title="Invalid match",
                content='<h1>Invalid match id</h1><p><a href="/watch">← Back</a></p>',
            ), 400)
        m = self.matches.get(match_id)
        # Try DB if not in memory (match completed)
        completed_row = None
        if not m:
            try:
                completed_row = self.db.execute(
                    "SELECT p1, p2, winner, p1_elo_delta, p2_elo_delta, created_at, completed_at "
                    "FROM matches WHERE id=?", (match_id,),
                ).fetchone()
            except Exception:
                pass
            if not completed_row:
                return (BASE_HTML.format(
                    title="Match not found",
                    content='<h1>Match not found</h1><p><a href="/watch">← Back</a></p>',
                ), 404)

        if m:
            elapsed = int(time.time() - m.created_at)
            mins, secs = divmod(elapsed, 60)
            live_html = ""
            if m.live_state and (time.time() - m.live_state_at) < 30:
                # Live HP dashboard
                hp1_pct = max(0, min(100, m.live_state["hp1"]))
                hp2_pct = max(0, min(100, m.live_state["hp2"]))
                rnd = m.live_state["round"]
                live_html = f"""
<div style="margin:2rem 0;padding:1.5rem;background:#161b22;border:1px solid #30363d;border-radius:10px">
  <div style="display:flex;justify-content:space-between;margin-bottom:1rem;font-weight:700">
    <span>{_html.escape(m.user_a)}</span>
    <span style="color:#ff3030">Round {rnd}</span>
    <span>{_html.escape(m.user_b)}</span>
  </div>
  <div style="display:flex;gap:1rem;align-items:center">
    <div style="flex:1;height:24px;background:#0d1117;border:1px solid #30363d;border-radius:4px;overflow:hidden;direction:rtl">
      <div style="height:100%;width:{hp1_pct}%;background:linear-gradient(90deg,#f85149,#ffb84d);transition:width .3s"></div>
    </div>
    <div style="font-family:Consolas,monospace;color:#8b949e">{hp1_pct}% / {hp2_pct}%</div>
    <div style="flex:1;height:24px;background:#0d1117;border:1px solid #30363d;border-radius:4px;overflow:hidden">
      <div style="height:100%;width:{hp2_pct}%;background:linear-gradient(90deg,#f85149,#ffb84d);transition:width .3s"></div>
    </div>
  </div>
</div>
"""
            else:
                live_html = (
                    '<p class="sub" style="margin-top:1rem">'
                    '<span data-i18n-en>Live state stream not received yet — client may not be sending STATE updates.</span>'
                    '<span data-i18n-es>Stream de estado aún no recibido — el cliente puede no estar enviando updates STATE.</span></p>'
                )
            content = f"""
<p class="back"><a href="/watch">← <span data-i18n-en>All matches</span><span data-i18n-es>Todas las peleas</span></a></p>
<h1>{_html.escape(m.user_a)} <span style="color:#8b949e;font-weight:400">vs</span> {_html.escape(m.user_b)}</h1>
<p class="sub">
  <code>{_html.escape(match_id)}</code> ·
  <span data-i18n-en>Duration</span><span data-i18n-es>Duración</span>: {mins:02d}:{secs:02d}
</p>
{live_html}
<script>setTimeout(function(){{location.reload();}}, 3000);</script>
"""
        else:
            p1, p2, winner, d1, d2, created, completed = completed_row
            dur = (completed or created) - created
            mins, secs = divmod(dur, 60)
            if winner == "TIE":
                outcome = '<span class="tie">TIE</span>'
            elif winner == "DISPUTED":
                outcome = '<span class="disp">DISPUTED</span>'
            elif winner:
                outcome = f'{_html.escape(winner)} <span class="win">WIN</span>'
            else:
                outcome = '<span class="disp">No result reported</span>'
            content = f"""
<p class="back"><a href="/watch">← <span data-i18n-en>All matches</span><span data-i18n-es>Todas las peleas</span></a></p>
<h1>{_html.escape(p1)} <span style="color:#8b949e;font-weight:400">vs</span> {_html.escape(p2)}</h1>
<p class="sub"><code>{_html.escape(match_id)}</code></p>
<div class="stats-row">
  <div class="stat"><div class="val">{outcome}</div>
    <div class="lbl"><span data-i18n-en>Result</span><span data-i18n-es>Resultado</span></div></div>
  <div class="stat"><div class="val">{mins:02d}:{secs:02d}</div>
    <div class="lbl"><span data-i18n-en>Duration</span><span data-i18n-es>Duración</span></div></div>
  <div class="stat"><div class="val">{('+' if d1 >= 0 else '')}{d1}</div>
    <div class="lbl">{_html.escape(p1)} ELO Δ</div></div>
  <div class="stat"><div class="val">{('+' if d2 >= 0 else '')}{d2}</div>
    <div class="lbl">{_html.escape(p2)} ELO Δ</div></div>
</div>
"""
        return (BASE_HTML.format(title=f"Match {match_id[:8]} — 3SX", content=content), 200)

    def _render_user_page(self, username: str) -> tuple[str, int]:
        """Returns (html, status). Status 404 if user not found."""
        import html as _html
        row = self.db.execute(
            "SELECT username, elo, wins, losses, created_at, last_login_at "
            "FROM users WHERE username=?", (username,),
        ).fetchone()
        if not row:
            return (BASE_HTML.format(
                title="Player not found",
                content='<h1>Player not found</h1><p class="sub"><a href="/">← Back to leaderboard</a></p>',
            ), 404)
        u, elo, wins, losses, created, last_login = row
        total = wins + losses
        rank = sf6_rank(elo, total)
        color = TIER_COLOR.get(rank["tier"], "#c9d1d9")
        wr = (wins * 100 // total) if total else 0
        elo_disp = str(elo) if total >= PLACEMENT_MATCHES else "—"

        # Match history
        rows_cur = self.db.execute(
            "SELECT id, p1, p2, winner, p1_elo_delta, p2_elo_delta, created_at, completed_at "
            "FROM matches WHERE (p1=? OR p2=?) AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 50",
            (username, username),
        )
        history_rows = []
        for mid, p1, p2, winner, d1, d2, created_at, completed_at in rows_cur:
            is_p1 = (p1 == username)
            opp = p2 if is_p1 else p1
            opp_link = f'<a href="/user/{_html.escape(opp)}">{_html.escape(opp)}</a>'
            my_delta = d1 if is_p1 else d2
            if winner == "TIE":
                outcome = '<span class="tie">TIE</span>'
                delta_disp = "±0"
            elif winner == "DISPUTED":
                outcome = '<span class="disp">DISPUTED</span>'
                delta_disp = "—"
            elif winner == username:
                outcome = '<span class="win">WIN</span>'
                delta_disp = f"+{my_delta}" if my_delta >= 0 else str(my_delta)
            else:
                outcome = '<span class="loss">LOSS</span>'
                delta_disp = str(my_delta) if my_delta < 0 else f"+{my_delta}"
            ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(completed_at or created_at))
            history_rows.append(
                f'<tr><td>{ts}</td><td>vs {opp_link}</td>'
                f'<td>{outcome}</td><td class="elo">{delta_disp}</td></tr>'
            )
        if not history_rows:
            history_rows.append('<tr><td colspan="4" class="empty">No completed matches yet.</td></tr>')

        return (BASE_HTML.format(
            title=f"{username} — 3SX Netplay",
            content=f"""
<p class="back"><a href="/">← Back to leaderboard</a></p>
<h1>{_html.escape(u)}</h1>
<p class="sub"><span class="rnk" style="color:{color}">{_html.escape(rank['label'])}</span></p>
<div class="stats-row">
  <div class="stat"><div class="val">{elo_disp}</div><div class="lbl">ELO</div></div>
  <div class="stat"><div class="val">{wins}</div><div class="lbl">Wins</div></div>
  <div class="stat"><div class="val">{losses}</div><div class="lbl">Losses</div></div>
  <div class="stat"><div class="val">{wr}%</div><div class="lbl">Win Rate</div></div>
</div>
<h2>Match History</h2>
<table>
<thead><tr><th>Date (UTC)</th><th>Opponent</th><th>Result</th><th>ELO Δ</th></tr></thead>
<tbody>{"".join(history_rows)}</tbody></table>
""",
        ), 200)

    async def handle_stats_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            req = request_line.decode("utf-8", errors="ignore").strip()
            # Drain headers, capturing If-None-Match and Content-Length
            request_if_none_match = ""
            request_content_length = 0
            request_method = "GET"
            request_authorization = ""
            try:
                request_method = req.split(" ", 1)[0].upper()
            except Exception:
                pass
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("utf-8", errors="ignore")
                if decoded.lower().startswith("if-none-match:"):
                    request_if_none_match = decoded.split(":", 1)[1].strip()
                elif decoded.lower().startswith("content-length:"):
                    try:
                        request_content_length = int(decoded.split(":", 1)[1].strip())
                    except Exception:
                        pass
                elif decoded.lower().startswith("authorization:"):
                    request_authorization = decoded.split(":", 1)[1].strip()
            # Route
            path = "/"
            try:
                _, path, _ = req.split(" ", 2)
            except Exception:
                pass

            import json as _json
            import urllib.parse

            # === Internal inter-server API (auth + result forwarding) ===
            if path.startswith("/api/internal/"):
                expected = f"Bearer {INTERNAL_SECRET}"
                if not hmac.compare_digest(request_authorization, expected):
                    _body = b'{"ok":false,"reject":"forbidden"}'
                    _rh = (
                        "HTTP/1.1 403 Forbidden\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(_body)}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    writer.write(_rh + _body)
                    await writer.drain()
                    return
                if request_method != "POST":
                    _body = b'{"ok":false,"reject":"method not allowed"}'
                    _rh = (
                        "HTTP/1.1 405 Method Not Allowed\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(_body)}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    writer.write(_rh + _body)
                    await writer.drain()
                    return
                body_bytes = b""
                if request_content_length > 0:
                    body_bytes = await asyncio.wait_for(reader.read(request_content_length), timeout=5.0)
                try:
                    req_body = _json.loads(body_bytes.decode("utf-8") or "{}")
                except Exception:
                    req_body = {}
                if path == "/api/internal/auth":
                    resp = await self._handle_internal_auth(req_body)
                elif path == "/api/internal/result":
                    resp = await self._handle_internal_result(req_body)
                else:
                    resp = {"ok": False, "reject": "unknown internal route"}
                _body = _json.dumps(resp).encode("utf-8")
                _rh = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(_body)}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("utf-8")
                writer.write(_rh + _body)
                await writer.drain()
                return

            # === Log upload route ===
            _LOG_USER_RE = re.compile(r"^/api/logs/([A-Za-z0-9_]{1,32})$")
            _log_m = _LOG_USER_RE.match(path)
            if _log_m:
                log_username = _log_m.group(1)
                if request_method not in ("POST",):
                    _body = b"Method Not Allowed"
                    _rh = (
                        "HTTP/1.1 405 Method Not Allowed\r\n"
                        "Allow: POST\r\n"
                        f"Content-Length: {len(_body)}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    writer.write(_rh + _body)
                    await writer.drain()
                    return
                MAX_LOG_BYTES = 1024 * 1024
                if request_content_length > MAX_LOG_BYTES:
                    _body = b"Payload Too Large"
                    _rh = (
                        "HTTP/1.1 413 Payload Too Large\r\n"
                        f"Content-Length: {len(_body)}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    writer.write(_rh + _body)
                    await writer.drain()
                    return
                read_len = request_content_length if request_content_length > 0 else MAX_LOG_BYTES
                log_data = await asyncio.wait_for(reader.read(read_len), timeout=10.0)
                if len(log_data) > MAX_LOG_BYTES:
                    _body = b"Payload Too Large"
                    _rh = (
                        "HTTP/1.1 413 Payload Too Large\r\n"
                        f"Content-Length: {len(_body)}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    writer.write(_rh + _body)
                    await writer.drain()
                    return
                logs_dir = "/opt/fistbump/logs"
                os.makedirs(logs_dir, exist_ok=True)
                log_file = os.path.join(logs_dir, f"{log_username}.log")
                with open(log_file, "wb") as _lf:
                    _lf.write(log_data)
                _resp = _json.dumps({"ok": True, "bytes": len(log_data)}).encode("utf-8")
                _rh = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(_resp)}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("utf-8")
                writer.write(_rh + _resp)
                await writer.drain()
                return

            # === Auto-updater routes ===
            _manifest_channel = None
            if path == "/api/update/stable.json":
                _manifest_channel = "stable"
            elif path == "/api/update/beta.json":
                _manifest_channel = "beta"
            if _manifest_channel is not None:
                manifest_path = f"/opt/fistbump/dist/{_manifest_channel}.json"
                try:
                    with open(manifest_path, "rb") as f:
                        body = f.read()
                    _stat = os.stat(manifest_path)
                    etag = f'"{_stat.st_mtime_ns}-{_stat.st_size}"'
                    if request_if_none_match == etag:
                        _rh = (
                            "HTTP/1.1 304 Not Modified\r\n"
                            f"ETag: {etag}\r\n"
                            "Cache-Control: max-age=60\r\n"
                            "Connection: close\r\n"
                            "\r\n"
                        ).encode("utf-8")
                        writer.write(_rh)
                        await writer.drain()
                        return
                    _rh = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        f"ETag: {etag}\r\n"
                        "Cache-Control: max-age=60\r\n"
                        "Access-Control-Allow-Origin: *\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    writer.write(_rh + body)
                    await writer.drain()
                    return
                except FileNotFoundError:
                    _body = b"manifest not found"
                    _rh = (
                        "HTTP/1.1 404 Not Found\r\n"
                        "Content-Type: text/plain\r\n"
                        f"Content-Length: {len(_body)}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    writer.write(_rh + _body)
                    await writer.drain()
                    return

            if path.startswith("/dl/"):
                name = path[4:]
                if not re.match(r"^3sx-\d+\.\d+\.\d+\.zip$", name):
                    _body = b"not found"
                    _rh = (
                        "HTTP/1.1 404 Not Found\r\n"
                        f"Content-Length: {len(_body)}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    writer.write(_rh + _body)
                    await writer.drain()
                    return
                file_path = f"/opt/fistbump/dist/{name}"
                if not os.path.isfile(file_path):
                    _body = b"not found"
                    _rh = (
                        "HTTP/1.1 404 Not Found\r\n"
                        f"Content-Length: {len(_body)}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    writer.write(_rh + _body)
                    await writer.drain()
                    return
                _stat = os.stat(file_path)
                etag = f'"{_stat.st_mtime_ns}-{_stat.st_size}"'
                if request_if_none_match == etag:
                    _rh = (
                        "HTTP/1.1 304 Not Modified\r\n"
                        f"ETag: {etag}\r\n"
                        "Cache-Control: max-age=300\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    writer.write(_rh)
                    await writer.drain()
                    return
                with open(file_path, "rb") as f:
                    body = f.read()
                _rh = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: application/zip\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"ETag: {etag}\r\n"
                    "Cache-Control: max-age=300\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("utf-8")
                writer.write(_rh + body)
                await writer.drain()
                return

            status = 200
            if path.startswith("/api"):
                stats = self._build_stats_dict()
                body = _json.dumps(stats).encode("utf-8")
                content_type = "application/json"
            elif path.startswith("/metrics"):
                stats = self._build_stats_dict()
                # Prometheus exposition format (text/plain)
                try:
                    completed = self.db.execute(
                        "SELECT COUNT(*) FROM matches WHERE completed_at IS NOT NULL"
                    ).fetchone()[0]
                    disputed = self.db.execute(
                        "SELECT COUNT(*) FROM matches WHERE winner='DISPUTED'"
                    ).fetchone()[0]
                except Exception:
                    completed = 0; disputed = 0
                lines = [
                    "# HELP fistbump_sessions_connected Current TCP sessions",
                    "# TYPE fistbump_sessions_connected gauge",
                    f"fistbump_sessions_connected {stats['connected']}",
                    "# HELP fistbump_queue_size Players currently in matchmaking queue (by mode)",
                    "# TYPE fistbump_queue_size gauge",
                    f'fistbump_queue_size{{mode="casual"}} {stats["queue_casual"]}',
                    f'fistbump_queue_size{{mode="ranked"}} {stats["queue_ranked"]}',
                    "# HELP fistbump_matches_pending Active (pre-completion) matches",
                    "# TYPE fistbump_matches_pending gauge",
                    f"fistbump_matches_pending {stats['matches_pending']}",
                    "# HELP fistbump_rooms_active Active private rooms",
                    "# TYPE fistbump_rooms_active gauge",
                    f"fistbump_rooms_active {len(self.rooms)}",
                    "# HELP fistbump_users_total Registered users in DB",
                    "# TYPE fistbump_users_total gauge",
                    f"fistbump_users_total {stats['users_total']}",
                    "# HELP fistbump_matches_completed_total Completed matches",
                    "# TYPE fistbump_matches_completed_total counter",
                    f"fistbump_matches_completed_total {completed}",
                    "# HELP fistbump_matches_disputed_total Matches flagged as DISPUTED (peer mismatch)",
                    "# TYPE fistbump_matches_disputed_total counter",
                    f"fistbump_matches_disputed_total {disputed}",
                    "# HELP fistbump_bans_total IPs in ban list",
                    "# TYPE fistbump_bans_total gauge",
                    f"fistbump_bans_total {stats['bans_count']}",
                ]
                body = ("\n".join(lines) + "\n").encode("utf-8")
                content_type = "text/plain; version=0.0.4; charset=utf-8"
            elif path.startswith("/watch"):
                # /watch → list active matches.  /watch/<match_id> → single match status.
                rest = path[6:].lstrip("/").split("?", 1)[0]
                if rest:
                    html, status = self._render_watch_match(rest)
                else:
                    html = self._render_watch_list()
                body = html.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            elif path.startswith("/user/"):
                name = urllib.parse.unquote(path[6:].split("?", 1)[0].rstrip("/"))
                # Validate username shape to avoid SQL surprises
                if not USERNAME_RE.match(name):
                    html, status = (BASE_HTML.format(
                        title="Invalid username",
                        content='<h1>Invalid username</h1><p class="sub"><a href="/">← Back</a></p>',
                    ), 400)
                else:
                    html, status = self._render_user_page(name)
                body = html.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            else:
                stats = self._build_stats_dict()
                body = self._render_leaderboard_html(stats).encode("utf-8")
                content_type = "text/html; charset=utf-8"

            status_text = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
            response_head = (
                f"HTTP/1.1 {status} {status_text}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8")
            writer.write(response_head + body)
            await writer.drain()
        except Exception as e:
            log.warning("stats http error: %s", e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ---------- Run ----------

    async def run(self):
        loop = asyncio.get_running_loop()

        # UDP listener
        class UDPProto(asyncio.DatagramProtocol):
            def __init__(self, srv):
                self.srv = srv

            def connection_made(self, transport):
                self.srv.udp_transport = transport
                log.info("UDP listening on :%d", self.srv.udp_port)

            def datagram_received(self, data, addr):
                asyncio.create_task(self.srv.handle_udp(data, addr))

        await loop.create_datagram_endpoint(
            lambda: UDPProto(self),
            local_addr=(self.host, self.udp_port),
        )

        class RelayProto(asyncio.DatagramProtocol):
            def __init__(self, srv):
                self.srv = srv
                self.transport = None
            def connection_made(self, transport):
                self.transport = transport
                self.srv.relay_transport = transport
                log.info("RELAY listening on :%d", self.srv.relay_port)
            def datagram_received(self, data, addr):
                asyncio.create_task(self.srv.handle_relay(data, addr))

        await loop.create_datagram_endpoint(
            lambda: RelayProto(self),
            local_addr=(self.host, self.relay_port),
        )

        # Janitor task
        asyncio.create_task(self.janitor_loop())

        # Stats HTTP listener (TCP port + 1000). Bind to 127.0.0.1 only —
        # expected to sit behind a reverse proxy (e.g. nginx + TLS).
        stats_port = self.tcp_port + 1000
        stats_server = await asyncio.start_server(
            self.handle_stats_http, "127.0.0.1", stats_port
        )
        log.info("Stats HTTP listening on 127.0.0.1:%d (private — nginx proxy only)", stats_port)
        asyncio.create_task(stats_server.serve_forever())

        # TCP listener
        tcp_server = await asyncio.start_server(
            self.handle_client, self.host, self.tcp_port
        )
        log.info("TCP listening on :%d", self.tcp_port)
        async with tcp_server:
            await tcp_server.serve_forever()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--tcp-port", type=int, default=9000)
    p.add_argument("--udp-port", type=int, default=9001)
    p.add_argument("--relay-port", type=int, default=19002)
    p.add_argument("--public-host", default="127.0.0.1",
                   help="Public IP advertised to clients in START")
    p.add_argument("--upstream-url", default=None,
                   help="If set, proxies auth + match results to this base URL. "
                        "Edge regions use this to share a single source-of-truth "
                        "leaderboard hosted by the leader region.")
    p.add_argument("--region-code", default="unknown",
                   help="Region tag stamped on match records (e.g. us-east-1)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    srv = FistbumpServer(args.tcp_port, args.udp_port, args.relay_port, args.host, args.public_host, args.upstream_url, args.region_code)
    try:
        asyncio.run(srv.run())
    except KeyboardInterrupt:
        log.info("shutdown")


if __name__ == "__main__":
    main()
