#!/usr/bin/env bash
# Publish a new client version to a release channel.
#
# Usage:
#   bash publish_update.sh <version> <zip-path> [stable|beta]
#
# Required env vars:
#   HOST            SSH target, e.g. ubuntu@fistbump.example.com
#   PUBLIC_BASE_URL Base URL the launcher hits for zip downloads,
#                   e.g. https://fistbump.example.com
#
# Optional env vars:
#   SSH_KEY         SSH private key path (default ~/.ssh/fistbump)
#   REMOTE_DIR      Remote install path (default /opt/fistbump)
#
# Copy publish_update.env.example to publish_update.env, fill in your values,
# and `source publish_update.env` before invoking.
set -euo pipefail

VERSION="${1:?usage: $0 <version> <zip-path> [stable|beta]}"
ZIP="${2:?usage: $0 <version> <zip-path> [stable|beta]}"
CHANNEL="${3:-stable}"

: "${HOST:?HOST env var required (e.g. ubuntu@fistbump.example.com)}"
: "${PUBLIC_BASE_URL:?PUBLIC_BASE_URL env var required (e.g. https://fistbump.example.com)}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fistbump}"
REMOTE_DIR="${REMOTE_DIR:-/opt/fistbump}"

if [ ! -f "$ZIP" ]; then
    echo "zip not found: $ZIP" >&2
    exit 1
fi
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "version must be SEMVER (got '$VERSION')" >&2
    exit 1
fi
if [[ "$CHANNEL" != "stable" && "$CHANNEL" != "beta" ]]; then
    echo "channel must be 'stable' or 'beta' (got '$CHANNEL')" >&2
    exit 1
fi

SHA=$(sha256sum "$ZIP" | awk '{print $1}')
SIZE=$(stat -c%s "$ZIP" 2>/dev/null || stat -f%z "$ZIP")
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DST_ZIP="3sx-${VERSION}.zip"
MANIFEST_NAME="${CHANNEL}.json"

TMP=$(mktemp -d)
cat > "$TMP/${MANIFEST_NAME}" <<EOF
{
  "schema": 1,
  "channel": "${CHANNEL}",
  "version": "${VERSION}",
  "released_at": "${TS}",
  "payload": {
    "url": "${PUBLIC_BASE_URL}/dl/${DST_ZIP}",
    "size_bytes": ${SIZE},
    "sha256": "${SHA}"
  },
  "notes_md": "",
  "notes_url": null
}
EOF

echo "Publishing ${VERSION} → channel=${CHANNEL}"
echo "  sha256: ${SHA}"
echo "  size:   ${SIZE}"

scp -i "$SSH_KEY" "$ZIP" "${HOST}:/tmp/${DST_ZIP}"
scp -i "$SSH_KEY" "$TMP/${MANIFEST_NAME}" "${HOST}:/tmp/${MANIFEST_NAME}"
ssh -i "$SSH_KEY" "$HOST" "
  sudo mkdir -p ${REMOTE_DIR}/dist &&
  sudo mv /tmp/${DST_ZIP} ${REMOTE_DIR}/dist/ &&
  sudo mv /tmp/${MANIFEST_NAME} ${REMOTE_DIR}/dist/${MANIFEST_NAME} &&
  sudo chown -R fistbump:fistbump ${REMOTE_DIR}/dist
"
rm -rf "$TMP"
echo "Published ${VERSION} on ${CHANNEL}"
