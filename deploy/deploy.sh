#!/usr/bin/env bash
# Copy the project to a Raspberry Pi over SSH and install or update the
# service there. Run from your own machine:
#
#   deploy/deploy.sh [--lan] [user@]host [remote-dir]
#
# The service listens on the Pi's localhost only, for an agent running on the
# Pi itself; --lan opens it to the network on first install. remote-dir
# defaults to ~/icloud-calendar-mcp. On the first deploy your local
# .env (if any) is sent once to seed /etc/icloud-calendar-mcp.env on the Pi,
# then deleted there; otherwise the installer prompts for credentials.
set -euo pipefail

INSTALL_ARGS=""
if [ "${1:-}" = --lan ]; then INSTALL_ARGS="--lan"; shift; fi
TARGET="${1:?usage: deploy/deploy.sh [--lan] [user@]host [remote-dir]}"
REMOTE_DIR="${2:-icloud-calendar-mcp}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ssh "$TARGET" true \
  || { echo "Cannot SSH to $TARGET. Check that 'ssh $TARGET' works on its own first." >&2; exit 1; }
ssh "$TARGET" 'command -v rsync >/dev/null' \
  || { echo "rsync is missing on $TARGET. Install it with: ssh $TARGET sudo apt install -y rsync" >&2; exit 1; }

echo "==> Copying project to $TARGET:$REMOTE_DIR"
rsync -az --delete \
  --exclude .git --exclude .venv --exclude .env \
  --exclude __pycache__ --exclude .pytest_cache \
  "$ROOT/" "$TARGET:$REMOTE_DIR/"

ENV_ARGS="$INSTALL_ARGS"
if ! ssh "$TARGET" 'test -f /etc/icloud-calendar-mcp.env' && [ -f "$ROOT/.env" ]; then
  echo "==> Sending local .env to seed the Pi's configuration"
  ssh "$TARGET" 'umask 077 && cat > "$HOME/.icloud-calendar-mcp.env.seed"' < "$ROOT/.env"
  ENV_ARGS="$ENV_ARGS"' --env-from "$HOME/.icloud-calendar-mcp.env.seed"'
fi

# -t so sudo on the Pi can ask for a password.
ssh -t "$TARGET" "bash \"$REMOTE_DIR/deploy/install.sh\" $ENV_ARGS"
