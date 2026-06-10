#!/usr/bin/env bash
# Builds a complete distributable: 3sx.exe (game) + launcher.exe + bootstrap +
# DLLs + shaders. Produces:
#   - dist/install-<ver>/                 ready-to-extract install
#   - dist/3sx-<ver>.zip                  what publish_update.sh uploads
#
# Usage: bash build_dist.sh <version>
set -euo pipefail

VERSION="${1:?usage: $0 <version>}"
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "version must be SEMVER (got '$VERSION')" >&2
    exit 1
fi

export PATH="/c/msys64/mingw64/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"      # …/3sx-fistbump-server
CLIENT_ROOT="$(cd "$REPO_ROOT/../3sx" && pwd)"
DIST_ROOT="$(cd "$REPO_ROOT/../.." && pwd)/dist"
mkdir -p "$DIST_ROOT"

# 1. Build 3sx.exe
echo "[1/4] Building 3sx.exe"
cd "$CLIENT_ROOT"
if [ ! -d build ]; then
    CC=clang CXX=clang++ cmake -B build -DCMAKE_BUILD_TYPE=Release -G Ninja
fi
cmake --build build --parallel
cmake --install build --prefix build/application > /dev/null

# 2. Build launcher exe
echo "[2/4] Building launcher exe"
cd "$REPO_ROOT/launcher"
rm -rf build dist
pyinstaller --clean 3sx_launcher_online.spec > /dev/null

# 3. Build bootstrap exe (preserves launcher dist by renaming intermediate dirs)
echo "[3/4] Building bootstrap exe"
# Move the just-built launcher build/dist out of the way so PyInstaller
# doesn't wipe them on bootstrap build.
mv dist dist-launcher
mv build build-launcher
pyinstaller --clean bootstrap.spec > /dev/null
mv dist dist-bootstrap
# Restore launcher artifacts.
mv dist-launcher dist
mv build-launcher build
# Bootstrap goes into the regular dist dir alongside the launcher exe.
cp dist-bootstrap/3SX.exe dist/3SX.exe
rm -rf dist-bootstrap

# 4. Assemble install layout
echo "[4/4] Assembling install layout"
INSTALL="$DIST_ROOT/install-$VERSION"
rm -rf "$INSTALL"
mkdir -p "$INSTALL/versions/$VERSION"
cp -r "$CLIENT_ROOT/build/application/bin/"* "$INSTALL/versions/$VERSION/"
cp "$REPO_ROOT/launcher/dist/3sx_launcher_online.exe" "$INSTALL/versions/$VERSION/"
cp "$REPO_ROOT/launcher/dist/3SX.exe" "$INSTALL/"
echo "$VERSION" > "$INSTALL/current.txt"

ZIP="$DIST_ROOT/3sx-$VERSION.zip"
rm -f "$ZIP"
(
    cd "$INSTALL"
    python -c "
import zipfile, os, sys
out = sys.argv[1]
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for root, _, files in os.walk('.'):
        for f in files:
            full = os.path.join(root, f)
            arc = os.path.relpath(full, '.')
            z.write(full, arc)
print('zip:', os.path.getsize(out), 'bytes')
" "$ZIP"
)

echo "Built install layout: $INSTALL"
echo "Built zip:            $ZIP"
echo ""
echo "Next steps:"
echo "  Test locally:   extract $ZIP somewhere clean, double-click 3SX.exe"
echo "  Publish:        bash publish_update.sh $VERSION $ZIP"
