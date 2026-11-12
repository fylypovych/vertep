#!/usr/bin/env bash
# Backup Worker role hook: configure backup directories and schedules.
set -euo pipefail
ROOT=$1
ROLE=$2
if [[ "$ROLE" != "backup" ]]; then
  exit 0
fi
echo "Configuring Backup Worker..."
install -d -m 0750 "$ROOT/backups"
install -d -m 0750 "$ROOT/storage/backups"
if [[ -f "$ROOT/.env" ]]; then
  python3 "$ROOT/scripts/set-env.py" "$ROOT/.env" BACKUP_ROOT "$ROOT/backups" || true
  python3 "$ROOT/scripts/set-env.py" "$ROOT/.env" BACKUP_SCHEDULE "0 2 * * *" || true
fi
