#!/usr/bin/env bash
# Publisher Worker role hook: configure publisher directories.
set -euo pipefail
ROOT=$1
ROLE=$2
if [[ "$ROLE" != "publisher" ]]; then
  exit 0
fi
echo "Configuring Publisher Worker..."
install -d -m 0750 "$ROOT/storage/published"
install -d -m 0750 "$ROOT/config/publisher"
if [[ -f "$ROOT/.env" ]]; then
  python3 "$ROOT/scripts/set-env.py" "$ROOT/.env" PUBLISHER_ROOT "$ROOT/storage/published" || true
fi
