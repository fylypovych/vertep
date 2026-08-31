#!/usr/bin/env bash
# Monitoring Worker role hook: configure monitoring directories.
set -euo pipefail
ROOT=$1
ROLE=$2
if [[ "$ROLE" != "monitoring" ]]; then
  exit 0
fi
echo "Configuring Monitoring Worker..."
install -d -m 0750 "$ROOT/monitoring"
install -d -m 0750 "$ROOT/monitoring/prometheus"
install -d -m 0750 "$ROOT/monitoring/grafana"
install -d -m 0750 "$ROOT/monitoring/loki"
if [[ -f "$ROOT/.env" ]]; then
  python3 "$ROOT/scripts/set-env.py" "$ROOT/.env" GRAFANA_ROOT "$ROOT/monitoring/grafana" || true
  python3 "$ROOT/scripts/set-env.py" "$ROOT/.env" PROMETHEUS_ROOT "$ROOT/monitoring/prometheus" || true
fi
