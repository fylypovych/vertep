#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
backup=${1:-}
[[ -n "$backup" && -f "$backup" ]] || { echo "Usage: restore.sh <jobs.tar.gz|postgres.sql>" >&2; exit 2; }
case "$backup" in
  *.tar.gz) docker compose -f "$root/docker-compose.yml" exec -T core tar -xzf - -C / < "$backup" ;;
  *.sql) docker compose -f "$root/docker-compose.yml" exec -T postgres psql -v ON_ERROR_STOP=1 -U vertep vertep < "$backup" ;;
  *) echo "Unsupported backup type" >&2; exit 2 ;;
esac
echo "Restore completed: $backup"
