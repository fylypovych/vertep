#!/usr/bin/env bash
# Text Processing Worker role hook: configure text processing directories.
set -euo pipefail
ROOT=$1
ROLE=$2
if [[ "$ROLE" != "text" ]]; then
  exit 0
fi
echo "Configuring Text Processing Worker..."
install -d -m 0750 "$ROOT/storage/text"
install -d -m 0750 "$ROOT/storage/text/raw"
install -d -m 0750 "$ROOT/storage/text/processed"
if [[ -f "$ROOT/.env" ]]; then
  python3 "$ROOT/scripts/set-env.py" "$ROOT/.env" TEXT_STORAGE "$ROOT/storage/text" || true
  python3 "$ROOT/scripts/set-env.py" "$ROOT/.env" TEXT_PROCESSOR "default" || true
fi
