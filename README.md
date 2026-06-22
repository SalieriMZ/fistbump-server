# fistbump-server

Matchmaking + ELO + lobby + relay server for [`SalieriMZ/3sx-online`](https://github.com/SalieriMZ/3sx-online) — the cross-platform rollback netplay fork of [crowded-street/3sx](https://github.com/crowded-street/3sx).

Single-process Python 3.11+ asyncio server. SQLite for accounts + match history. No external services required to run locally; production drops a systemd unit on a single Linux box and points the client at it via `regions.txt`.

## What it does

| Surface | Purpose |
|---|---|
| TCP 19000 | Line-protocol signaling: `VERSION / HELLO / REGISTER / LOGIN / REFRESH / SET / QUEUE / ROOM / CHAT / RESULT / STATE / DECLINE` (`VERSION` is a pre-auth update check). Server-pushed: `SESSION / TOKEN / PROFILE / MATCH / START / REJECT / CANCEL / LOADING`. |
| UDP 19001 | NAT-punch + endpoint discovery. Server captures the public `(ip, port)` of each peer's punch packet, dispatches them at `START` time. |
| UDP 19002 | Relay fallback for CGNAT / symmetric-NAT peers. 36-char match-id prefix + 1-char side selector routes packets. |
| HTTP 20000 (loopback) | Stats JSON + live-match viewer + leaderboard. Expected behind nginx + TLS (sample at `nginx/fistbump.example.com.conf.example`). |

Auth: HMAC-SHA256 refresh tokens, PBKDF2-SHA256 password hashing (200k iterations). Rate limited per IP for connect / login / queue / chat. Soft-banlist via `bans.json`.

Multi-region: edge nodes can forward `REGISTER / LOGIN / REFRESH / RESULT` to a leader node via `--upstream-url` so every region writes to one source-of-truth leaderboard.

## Quick start

```sh
python3 server.py --tcp-port 19000 --udp-port 19001 -v
```

On another machine (or the same one), point a client at it by dropping a `regions.txt` somewhere the client reads:

```
# code|label|host|port
local|Local|127.0.0.1|19000
```

See [`SalieriMZ/3sx-online`](https://github.com/SalieriMZ/3sx-online) for client-side configuration.

### Testing without the game

Spin up two anonymous sessions (server has to be started with `FISTBUMP_ALLOW_ANON=1`):

```sh
FISTBUMP_ALLOW_ANON=1 python3 server.py --tcp-port 19000 --udp-port 19001 -v
```

then attach raw TCP clients:

```sh
nc localhost 19000
> HELLO 1.4.0
< SESSION <sid>
> QUEUE casual
...
```

## Deploy

`deploy.sh` provisions a generic Ubuntu host: creates the `fistbump` system user, drops `server.py` + `fistbump.service` + `/etc/default/fistbump`, restarts the service.

```sh
REMOTE_HOST=fistbump.example.com \
SSH_KEY=~/.ssh/fistbump \
PUBLIC_HOST=fistbump.example.com \
bash deploy.sh
```

`fistbump.service` reads `PUBLIC_HOST` from `/etc/default/fistbump` and passes it via `--public-host`. Clients in the **direct** path are told to UDP-punch this address.

### Docker

```sh
docker build -t fistbump .
docker run -d --name fistbump \
    -e FISTBUMP_ALLOW_ANON=0 \
    -p 19000:19000/tcp \
    -p 19001:19001/udp \
    -p 19002:19002/udp \
    --restart=unless-stopped \
    fistbump
```

### Behind nginx + TLS

Copy `nginx/fistbump.example.com.conf.example` to your sites-enabled, replace the hostname, run certbot. Only the stats HTTP (loopback `127.0.0.1:20000`) goes through nginx; matchmaking TCP/UDP stay direct.

## Publishing client updates

Updates are checked **in-game**, not by a launcher. On connect the client sends a
pre-auth `VERSION` command on the TCP line; the server answers on one line with
`VERSION latest <latest_ver> <url>` — `LATEST_CLIENT_VERSION` plus the public
GitHub `releases/latest` URL. The game compares that to its baked build version
and tells the player either that they're up to date or that an update is
available (opening the GitHub releases page). The matchmaking host never serves
game binaries or update manifests.

To ship a release: cut a GitHub release (`stable-x.y.z`, marked *Latest*), then on
the server bump `ALLOWED_VERSIONS` (and `SERVER_VERSION` / `LATEST_CLIENT_VERSION`)
in `server.py`. Older clients fall out of `ALLOWED_VERSIONS`, get rejected, and
the in-game update check points them at the new release.

## Repo layout

```
server.py                  Single-file asyncio server (~2.7k LOC).
fistbump.service           Systemd unit, reads /etc/default/fistbump.
deploy.sh                  Generic SSH deploy of server.py + unit.
Dockerfile                 Alpine + python3 + server.py copy.
nginx/                     Reverse-proxy template.
```

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `FISTBUMP_ALLOW_ANON` | `0` | Allow `HELLO` without credentials (dev/local only). |
| `FISTBUMP_RATE_WHITELIST` | empty | Comma-separated IPs exempt from per-IP rate limits. |
| `--public-host` / `$PUBLIC_HOST` | `127.0.0.1` | Public IP/hostname advertised to clients in `START`. |
| `--tcp-port` | `19000` | Signaling port. |
| `--udp-port` | `19001` | NAT-punch + match-data port (reused as the GekkoNet socket on the client side). |
| `--relay-port` | `19002` | Relay UDP port for CGNAT peers. |
| `--upstream-url` | unset | Edge mode: HTTPS base URL of the leader for auth + result forwarding. |

## License

AGPL-3.0 — same as the [3sx-online](https://github.com/SalieriMZ/3sx-online) client. See [`LICENSE`](LICENSE).

## Acknowledgements

- [crowded-street/3sx](https://github.com/crowded-street/3sx) — the decompilation that the netplay client builds on.
- [GekkoNet](https://github.com/HeatXD/GekkoNet) — the rollback library used by the client.
