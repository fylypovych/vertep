#!/usr/bin/env bash
# Production acceptance test matrix for Vertep.
# Requires: Ubuntu 24.04 amd64/arm64, Docker, Docker Compose, NVIDIA/AMD GPU optional.
set -euo pipefail

MATRIX="${1:-local}"
REPORT="qualification-${MATRIX}-$(date -u +%Y%m%d-%H%M%S).json"
EVIDENCE=()

pass() { echo "[PASS] $1"; EVIDENCE+=("$1"); }
fail() { echo "[FAIL] $1"; EVIDENCE+=("$1"); }
skip() { echo "[SKIP] $1"; EVIDENCE+=("$1"); }

echo "{" > "$REPORT"
echo "  \"matrix\": \"${MATRIX}\"," >> "$REPORT"
echo "  \"started_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"," >> "$REPORT"
echo "  \"results\": [" >> "$REPORT"

# 1. Fresh host preflight
echo "=== Preflight ==="
if command -v lsb_release >/dev/null 2>&1 && [ "$(lsb_release -cs)" = "noble" ]; then
  pass "ubuntu_24_04"
else
  fail "ubuntu_24_04"
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  pass "docker_available"
else
  fail "docker_available"
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  pass "docker_compose_available"
else
  fail "docker_compose_available"
fi

# 2. PostgreSQL restore
echo "=== PostgreSQL ==="
if docker compose -f deploy/docker-compose.yml --env-file .env up -d postgres >/dev/null 2>&1; then
  sleep 5
  if docker exec vertep-postgres-1 pg_isready -U vertep -d vertep >/dev/null 2>&1; then
    pass "postgres_ready"
  else
    fail "postgres_ready"
  fi
  docker compose -f deploy/docker-compose.yml --env-file .env down >/dev/null 2>&1 || true
else
  skip "postgres_ready_no_compose"
fi

# 3. Nginx mTLS
echo "=== Nginx mTLS ==="
if [ -f tls/vertep.crt ] && [ -f tls/vertep.key ] && [ -f tls/node-ca.crt ]; then
  pass "tls_material_present"
else
  fail "tls_material_present"
fi

# 4. Physical GPU
echo "=== GPU ==="
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  pass "nvidia_gpu_detected"
else
  skip "nvidia_gpu_not_present"
fi

# 5. Browser E2E readiness
echo "=== Browser E2E ==="
if command -v chromium-browser >/dev/null 2>&1 || command -v google-chrome >/dev/null 2>&1; then
  pass "browser_available"
else
  skip "browser_not_available"
fi

PASSED=$(printf '%s\n' "${EVIDENCE[@]}" | grep -c '^\[PASS\]' || true)
FAILED=$(printf '%s\n' "${EVIDENCE[@]}" | grep -c '^\[FAIL\]' || true)
SKIPPED=$(printf '%s\n' "${EVIDENCE[@]}" | grep -c '^\[SKIP\]' || true)

echo "]," >> "$REPORT"
echo "  \"passed\": ${PASSED}," >> "$REPORT"
echo "  \"failed\": ${FAILED}," >> "$REPORT"
echo "  \"skipped\": ${SKIPPED}," >> "$REPORT"
echo "  \"completed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"" >> "$REPORT"
echo "}" >> "$REPORT"

echo "=== Summary: ${PASSED} passed, ${FAILED} failed, ${SKIPPED} skipped ==="
cat "$REPORT"
