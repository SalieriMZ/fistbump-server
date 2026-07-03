"""Unit test for the per-game web match history (1.9.0 fix).

An in-game REMATCH series shares ONE match_id; the matches row is UPDATEd once
per game, so a matches-only history overwrites the earlier game. The user page
now reads per-game from the games table (UNION fallback to matches for rows with
no games entry: DISPUTED / pre-1.9.0). This test builds a DB with the shipping
schema and asserts the history query returns one row PER GAME plus the fallbacks.
"""
import os, sqlite3, tempfile, time

# --- shipping schema (mirrors server.py _init_db, games incl. elo_delta cols) ---
SCHEMA = """
CREATE TABLE matches (
    id TEXT PRIMARY KEY, p1 TEXT NOT NULL, p2 TEXT NOT NULL, winner TEXT,
    p1_elo_delta INTEGER DEFAULT 0, p2_elo_delta INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL, completed_at INTEGER,
    region TEXT, platform_a TEXT, platform_b TEXT,
    is_ranked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE games (
    id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL, game_no INTEGER NOT NULL,
    char_a INTEGER, char_b INTEGER, winner TEXT, is_ranked INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL, elo_delta_a INTEGER NOT NULL DEFAULT 0, elo_delta_b INTEGER NOT NULL DEFAULT 0
);
"""

# --- exact query from server.py _render_user_page (keep in sync) ---
HISTORY_SQL = (
    "SELECT p1, p2, winner, da, db, ts FROM ("
    "  SELECT m.p1 AS p1, m.p2 AS p2, g.winner AS winner,"
    "         g.elo_delta_a AS da, g.elo_delta_b AS db, g.ts AS ts"
    "    FROM games g JOIN matches m ON g.match_id = m.id"
    "   WHERE m.p1=? OR m.p2=?"
    "  UNION ALL"
    "  SELECT m.p1, m.p2, m.winner, m.p1_elo_delta, m.p2_elo_delta,"
    "         COALESCE(m.completed_at, m.created_at) AS ts"
    "    FROM matches m"
    "   WHERE (m.p1=? OR m.p2=?) AND m.completed_at IS NOT NULL"
    "     AND NOT EXISTS (SELECT 1 FROM games g2 WHERE g2.match_id = m.id)"
    ") ORDER BY ts DESC LIMIT 50"
)

fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
db = sqlite3.connect(path)
try:
    db.executescript(SCHEMA)
    t0 = 1_700_000_000

    # 1) Rematch series: ONE match_id, ranked, 3 games (A wins 2, B wins 1).
    db.execute("INSERT INTO matches (id,p1,p2,winner,p1_elo_delta,p2_elo_delta,created_at,completed_at,is_ranked)"
               " VALUES ('rematch','Salieri','Test','Salieri',12,-12,?,?,1)", (t0, t0 + 300))
    db.execute("INSERT INTO games (match_id,game_no,winner,is_ranked,ts,elo_delta_a,elo_delta_b)"
               " VALUES ('rematch',1,'Salieri',1,?,15,-15)", (t0 + 100,))
    db.execute("INSERT INTO games (match_id,game_no,winner,is_ranked,ts,elo_delta_a,elo_delta_b)"
               " VALUES ('rematch',2,'Test',1,?,-13,13)", (t0 + 200,))
    db.execute("INSERT INTO games (match_id,game_no,winner,is_ranked,ts,elo_delta_a,elo_delta_b)"
               " VALUES ('rematch',3,'Salieri',1,?,12,-12)", (t0 + 300,))

    # 2) DISPUTED match: matches row, NO games row -> must fall back.
    db.execute("INSERT INTO matches (id,p1,p2,winner,created_at,completed_at,is_ranked)"
               " VALUES ('disp','Salieri','Test','DISPUTED',?,?,1)", (t0 + 400, t0 + 450))

    # 3) Legacy match (pre-games-table): matches row, NO games row -> fall back.
    db.execute("INSERT INTO matches (id,p1,p2,winner,p1_elo_delta,p2_elo_delta,created_at,completed_at,is_ranked)"
               " VALUES ('legacy','Test','Salieri','Test',-8,8,?,?,1)", (t0 + 500, t0 + 550))
    db.commit()

    rows = db.execute(HISTORY_SQL, ("Salieri", "Salieri", "Salieri", "Salieri")).fetchall()

    # Expect 5 rows: 3 rematch games + disputed + legacy (NOT 3 overwritten to 1).
    assert len(rows) == 5, f"expected 5 history rows, got {len(rows)}: {rows}"

    # Newest first by ts: legacy(550) > disp(450) > g3(300) > g2(200) > g1(100).
    winners = [r[2] for r in rows]
    assert winners == ["Test", "DISPUTED", "Salieri", "Test", "Salieri"], winners

    # Per-game ELO deltas preserved distinctly (would be a single value if overwritten).
    # From Salieri's view (p1 in rematch): da column.
    rematch_deltas = [r[3] for r in rows if r[2] in ("Salieri", "Test") and r[5] in (t0+100, t0+200, t0+300)]
    assert sorted(rematch_deltas) == [-13, 12, 15], rematch_deltas

    # Old matches-only query would have collapsed the rematch to ONE row.
    old = db.execute("SELECT COUNT(*) FROM matches WHERE (p1=? OR p2=?) AND completed_at IS NOT NULL",
                     ("Salieri", "Salieri")).fetchone()[0]
    assert old == 3, old  # 3 matches rows total (rematch collapsed) -> proves the bug the fix addresses

    print(f"PASS: per-game history returns {len(rows)} rows (rematch split into 3 + disputed + legacy);")
    print(f"      matches-only would show only {old} rows (rematch overwritten to 1). Fix verified.")
finally:
    db.close()
    os.remove(path)
