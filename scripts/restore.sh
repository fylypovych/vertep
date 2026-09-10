#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
backup=${1:-}
[[ -n "$backup" && -f "$backup" ]] || { echo "Usage: restore.sh <backup.vtbackup|jobs.tar.gz|postgres.sql>" >&2; exit 2; }

# System-state gating: block restore in EMERGENCY, warn otherwise
# Tries to query CORE state; if CORE unreachable, allow with warning (standalone)
state=""
if command -v curl >/dev/null 2>&1; then
  state=$(curl -sf http://localhost:8080/api/system/state 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',''))" 2>/dev/null || true)
fi
if [[ "$state" == "EMERGENCY" ]]; then
  echo "Restore blocked: system is in EMERGENCY state" >&2
  exit 1
fi
if [[ -n "$state" && "$state" != "MAINTENANCE" && "$state" != "READ_ONLY" && "$state" != "RECOVERING" && "$state" != "NORMAL" && "$state" != "UNKNOWN" ]]; then
  echo "Warning: restore in state $state — use MAINTENANCE/READ_ONLY/RECOVERING" >&2
fi

case "$backup" in
  *.vtbackup)
    # Decrypt via backup-service API if available, else try local decrypt
    if curl -sf http://localhost:8092/health >/dev/null 2>&1; then
      snap_id=$(basename "$backup" .vtbackup)
      echo "Restoring snapshot $snap_id via backup-service..."
      curl -sf -X POST "http://localhost:8092/snapshots/${snap_id}/restore" | python3 -m json.tool
    else
      echo "Backup service not reachable — attempting direct tar restore" >&2
      # Fallback: treat as encrypted; require BACKUP_ENCRYPTION_KEY env
      echo "Direct .vtbackup restore requires running backup-service" >&2
      exit 1
    fi
    ;;
  *.tar.gz) docker compose -f "$root/docker-compose.yml" exec -T core tar -xzf - -C / < "$backup" ;;
  *.sql) docker compose -f "$root/docker-compose.yml" exec -T postgres psql -v ON_ERROR_STOP=1 -U vertep vertep < "$backup" ;;
  *) echo "Unsupported backup type" >&2; exit 2 ;;
esac

# Post-restore health check
echo "Running post-restore health check..."
if curl -sf http://localhost:8080/api/health >/dev/null 2>&1; then
  echo "Health check: OK"
else
  echo "Health check: WARNING — CORE not reachable after restore" >&2
fi
echo "Restore completed: $backup"
