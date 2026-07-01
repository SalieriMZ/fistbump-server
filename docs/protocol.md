# Fistbump wire protocol

The matchmaking server speaks a newline-delimited ASCII **line protocol** over
TCP for signaling, plus a tiny UDP framing for NAT punch + relay. This document
is the canonical reference; the authoritative source is `server.py`
(`handle_command` dispatch ~line 646) and the client `fistbump.c`.

Ports (defaults, all overridable):

| Port | Proto | Role |
|---|---|---|
| 19000 | TCP | Signaling (this document) |
| 19001 | UDP | NAT punch + match data (reused as the GekkoNet socket client-side) |
| 19002 | UDP | Relay for CGNAT/symmetric-NAT peers |

## Two-axis versioning (important)

There are **two related version numbers**:

- **Game protocol version** — the in-binary netcode/state protocol checked at
  login via `ALLOWED_VERSIONS` (`server.py:309`, currently `{"1.4.1"}`). Two
  clients can only cross-play if these match. Bumped only when the rollback state
  layout or the input/match wire format changes. Release 1.8.1 cut the protocol to
  `1.4.1` (rollback determinism fixes — a 1.8.0 client's save-state differs and
  would desync); only the current protocol is allowed, so every older client is
  rejected. (History: `1.3.1` from 1.7.28 through 1.7.29, `1.4.0` for 1.8.0.)
- **Build / app version** (currently `1.8.1`) — the shipped client build,
  reported to the **in-game update check** via the `VERSION` command (see below).
  The server replies with `LATEST_CLIENT_VERSION` + the GitHub releases URL; the
  game compares it to its baked build version and prompts the player. There is no
  longer a launcher that pulls releases.

A client sends its protocol version in `HELLO` / `LOGIN` / `REGISTER`; if it isn't
in `ALLOWED_VERSIONS` the server replies `REJECT version mismatch — allowed: …`,
and the in-game update check points the player at the new release.
**This gate is update-prompting, not anti-cheat** — a scripted client can send
any string. Result integrity instead comes from **two-peer concordance** (both
peers must report the same outcome or the match is DISPUTED and doesn't count).

## Connection lifecycle

```
client                              server
  | --- (TCP connect 19000) ------->|
  | --- HELLO <gamever> ----------->|
  | <-- SESSION <sid> <server_ver> -|   session id assigned
  | --- LOGIN <user> <pass> <ver> <platform> -->|
  | <-- TOKEN <jwt> ----------------|   (on success; refresh token)
  | <-- PROFILE <…> ----------------|   username, elo, stats
  | --- QUEUE add [mode] ---------->|   casual|ranked
  | <-- MATCH <…> ------------------|   opponent found
  | --- (UDP punch: see below) ---->|
  | <-- START <side> <relay_ep> <match_id> <use_relay> <direct> -->|
  | …gameplay over UDP (GekkoNet, peer↔peer or via relay)…         |
  | --- RESULT <my_wins> <opp_wins> ->|   after the set
```

`AWAITING_MATCH` (in queue) has no timeout — it can legitimately take minutes.
All other waiting states are bounded client-side (handshake watchdog in
`fistbump.c Fistbump_Run`).

## Client → server commands

| Command | Args | Notes |
|---|---|---|
| `VERSION` | — | Pre-auth in-game update check. No login required; doesn't close the connection. Server replies `VERSION latest <version> <url>` (`LATEST_CLIENT_VERSION` + GitHub releases URL). |
| `HELLO` | `<gamever>` | First line; server replies `SESSION <sid>`. |
| `REGISTER` | `<user> <pass> <ver> <platform>` | Creates account + logs in. |
| `LOGIN` | `<user> <pass> <ver> <platform>` | Password auth (PBKDF2). |
| `REFRESH` | `<token>` | Re-auth with a stored refresh JWT. |
| `SET` | `<key> <value>` | e.g. `force_relay 1`. |
| `QUEUE` | `add [mode]` / `remove` | `mode` = `casual` (default) or `ranked`. |
| `DECLINE` | `<match_id>` | Decline a found match. |
| `RESULT` | `<my_wins> <opp_wins>` | Each clamped 0..9; counted once per match. |
| `STATE` | `<…>` | Live in-match score stream (spectator/leaderboard). |
| `ROOM` | `CREATE <name>` / `JOIN <code>` / `LEAVE` / `SLOT <A\|B>` / `UNSLOT` / `START` / `SETTINGS <key> <value>` | Private rooms with codes. |
| `CHAT` | `<scope> <msg>` | `scope` = room/match. |

## Server → client pushes

| Push | Fields | Notes |
|---|---|---|
| `SESSION` | `<sid> <server_ver>` | Assigned session id; `server_ver` (`SERVER_VERSION`) is an additive token older clients ignore. |
| `TOKEN` | `<jwt>` | Refresh token to persist. |
| `PROFILE` | `<…>` | Username + stats (full username since 1.3.1). |
| `MATCH` | `<…>` | Opponent found; client then runs the UDP punch. |
| `START` | `<side> <relay_endpoint> <match_id> <use_relay> <direct>` | `side` ∈ {1,2}; `use_relay` 0/1; `direct` = peer endpoint for the P2P path (`server.py:1845`). |
| `LOADING` | `<0\|1>` | Peer's AFS-load signal (mid-match pause keepalive). |
| `CANCEL` | — | Peer left / match aborted; client tears the session down. |
| `REJECT` | `<reason>` | Command refused (version, auth, room state, …). |
| `ROOM STATE` | `<…>` | Room membership + slots + per-member wins broadcast. |

## UDP punch + relay framing

- **Punch** (client → server `:19001`): one datagram `"<id> <match_id>"`
  (`fistbump.c Fistbump_SendUDP`). The server records each side's source
  endpoint (`udp_endpoint`) for the relay binding.
- **Relay datagram** (client ↔ server `:19002`): first **36 bytes** = the match
  UUID, **37th byte** = side `'1'`/`'2'`, remainder = the GekkoNet payload. The
  server forwards to the other side's bound endpoint. Since the security pass,
  each side's relay route is **bound to the IP learned during the authenticated
  punch and locked after first use** — a spoofed source IP is rejected
  (`server.py handle_relay`).

## Data path

P2P-first: the server intersects the two peers' advertised `/24` LAN prefixes
to offer a LAN-direct fast path; otherwise direct over the punched endpoints;
relay only when a symmetric NAT defeats the punch (`force_relay`). The relay
carries the same GekkoNet UDP payload — it is a dumb forwarder, not an
authority. See `docs/protocol.md` consumers in `../3sx-online`
(`rollback-determinism.md`) for the gameplay side.
