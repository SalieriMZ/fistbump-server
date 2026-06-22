#!/usr/bin/env bash
# Deploy the fistbump matchmaking server to a generic Linux host.
#
# Required env:
#   REMOTE_HOST   target hostname or IP
#   REMOTE_USER   SSH login (default: ubuntu)
#   SSH_KEY       SSH private key (default: ~/.ssh/fistbump)
#
# Optional env:
#   REMOTE_DIR    install path on the server (default: /opt/fistbump)
#   PUBLIC_HOST   value baked into /etc/default/fistbump for the systemd unit
#                 (advertised to clients in START). Default: REMOTE_HOST.
#
# Opens the matchmaking TCP/UDP ports inside the systemd unit — be sure to
# also open them in any cloud-provider firewall (upstream server/EC2 SG/UFW).
set -euo pipefail

: "${REMOTE_HOST:?REMOTE_HOST env var required}"
REMOTE_USER="${REMOTE_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fistbump}"
REMOTE_DIR="${REMOTE_DIR:-/opt/fistbump}"
PUBLIC_HOST="${PUBLIC_HOST:-$REMOTE_HOST}"

# Arrays, not strings — so a space or special char in SSH_KEY / REMOTE_* is
# passed as one argument instead of being re-split by the shell.
SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "$REMOTE_USER@$REMOTE_HOST")
SCP=(scp -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)

echo "→ creating user + dir on $REMOTE_HOST"
"${SSH[@]}" "sudo useradd -r -s /usr/sbin/nologin fistbump 2>/dev/null || true; \
      sudo mkdir -p $REMOTE_DIR; \
      sudo chown fistbump:fistbump $REMOTE_DIR"

echo "→ uploading server.py"
"${SCP[@]}" server.py "$REMOTE_USER@$REMOTE_HOST:/tmp/server.py"
"${SSH[@]}" "sudo mv /tmp/server.py $REMOTE_DIR/server.py && sudo chown fistbump:fistbump $REMOTE_DIR/server.py"

echo "→ writing /etc/default/fistbump (PUBLIC_HOST=$PUBLIC_HOST)"
"${SSH[@]}" "echo 'PUBLIC_HOST=$PUBLIC_HOST' | sudo tee /etc/default/fistbump >/dev/null && sudo chmod 0644 /etc/default/fistbump"

echo "→ uploading systemd unit"
"${SCP[@]}" fistbump.service "$REMOTE_USER@$REMOTE_HOST:/tmp/fistbump.service"
"${SSH[@]}" "sudo mv /tmp/fistbump.service /etc/systemd/system/fistbump.service"

echo "→ enable + start"
"${SSH[@]}" "sudo systemctl daemon-reload && \
      sudo systemctl enable fistbump.service && \
      sudo systemctl restart fistbump.service && \
      sleep 1 && \
      sudo systemctl status fistbump.service --no-pager"

echo ""
echo "Done. Don't forget to open TCP 19000 + UDP 19001/19002 in your firewall."
echo "Test:  nc -v $REMOTE_HOST 19000"
