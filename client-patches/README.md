# Client Patches

Diffs aplicados al cliente [crowded-street/3sx](https://github.com/crowded-street/3sx) (branch `main`) para activar netplay funcional.

Aplicar:
```bash
git clone https://github.com/crowded-street/3sx
cd 3sx
for p in /path/to/3sx-netplay/client-patches/*.patch; do
    git apply "$p"
done
```

## Lista patches

| # | Archivo | Qué hace |
|---|---------|----------|
| 01 | `src/main.c` | Default upstream server server, read args (matchmaking-ip, p2p-*), wire `Netplay_TickDirectP2P/TickMatchmaking/Run` en main loop, skip `njUserMain` duplicado durante CONNECTING/RUNNING |
| 02 | `src/platform/netplay/netplay.c` | UDP port = TCP+1 (no hardcoded 9001), desync detection siempre activo, connect/peer-silent timeouts, EXITING bail-out en step_logic |
| 03 | `src/sf33rd/Source/Game/menu/netplay_menu.c` | Auto-FindMatch + Auto-AcceptMatch (UI menu Network está rota upstream) |
| 04 | `CMakeLists.txt` | NETPLAY_ENABLED también en Release, -Wno-unused-function en Release |

## Build (Windows MSYS2 MinGW64)

```bash
# Toolchain:
pacman -S --needed make mingw-w64-x86_64-cmake mingw-w64-x86_64-ninja \
    mingw-w64-x86_64-nasm mingw-w64-x86_64-clang mingw-w64-x86_64-zlib \
    mingw-w64-x86_64-headers-git mingw-w64-x86_64-git

# Deps (largo):
bash build-deps.sh

# Build Release:
CC=clang CXX=clang++ cmake -B build -DCMAKE_BUILD_TYPE=Release -G Ninja
cmake --build build --parallel --config Release
cmake --install build --prefix build/application
```

Binario en `build/application/bin/3sx.exe`.

## Override server runtime

```bash
3sx.exe --matchmaking-ip mi.server.com --matchmaking-port 19000
```

O env vars:
```bash
FISTBUMP_HOST=mi.server.com FISTBUMP_PORT=19000 ./3sx.exe
```

## Direct P2P sin server

```bash
# Player 1:
3sx.exe --p2p-local-player 1 --p2p-remote-ip 127.0.0.1

# Player 2:
3sx.exe --p2p-local-player 2 --p2p-remote-ip 127.0.0.1
```
