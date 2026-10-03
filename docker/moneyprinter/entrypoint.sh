#!/bin/sh
# Vertep MoneyPrinterTurbo container entrypoint (Issue #122 P2).
#
# Layout inside the image:
#   /opt/moneyprinter       pinned upstream MoneyPrinterTurbo sources
#   /opt/vertep             Vertep wrapper (services/ + adapters/)
#
# Responsibilities:
#   1. render the locked upstream config with the runtime API key;
#   2. assert the rendered config against the lock (fail closed on drift);
#   3. verify the runtime inventory the image build recorded;
#   4. start the pinned upstream API on loopback only;
#   5. serve the Vertep wrapper as the single external endpoint.

set -eu

APP_DIR="/opt/moneyprinter"
VERTEP_DIR="/opt/vertep"
CONFIG_LOCK="${APP_DIR}/config.lock.toml"
CONFIG_FILE="${APP_DIR}/config.toml"
INVENTORY_FILE="${APP_DIR}/runtime-inventory.json"
API_KEY_FILE="${MONEYPRINTER_API_KEY_FILE:-/run/secrets/moneyprinter_api_key}"
WRAPPER_PORT="${MONEYPRINTER_WRAPPER_PORT:-8098}"

log() {
    printf '[moneyprinter-entrypoint] %s\n' "$*" >&2
}

fail() {
    log "FATAL: $*"
    exit 1
}

# --- API key -----------------------------------------------------------------
# An empty key would disable upstream authentication entirely, which the
# readiness contract forbids. Fail closed instead of starting an open API.
[ -r "${API_KEY_FILE}" ] || fail "runtime API key file is not readable: ${API_KEY_FILE}"

API_KEY="$(tr -d '\r\n' < "${API_KEY_FILE}")"
[ -n "${API_KEY}" ] || fail "runtime API key is empty; refusing to start an unauthenticated API"

# --- Render the locked config ------------------------------------------------
[ -f "${CONFIG_LOCK}" ] || fail "configuration lock is missing: ${CONFIG_LOCK}"

python3 - "${CONFIG_LOCK}" "${CONFIG_FILE}" "${API_KEY}" <<'PY'
import sys

import toml

lock_path, config_path, api_key = sys.argv[1], sys.argv[2], sys.argv[3]
config = toml.load(lock_path)
config["app"]["api_key"] = api_key

with open(config_path, "w", encoding="utf-8") as handle:
    toml.dump(config, handle)
PY

# --- Assert the rendered config against the lock ------------------------------
# These assertions are release requirements, not conveniences: a value that
# silently reverted to an upstream default would either publish rendered videos to
# social networks or expose the runtime to unauthenticated callers.
python3 - "${CONFIG_LOCK}" "${CONFIG_FILE}" <<'PY'
import sys

import toml

lock = toml.load(sys.argv[1])
rendered = toml.load(sys.argv[2])


def locked(path):
    value = lock
    for part in path.split("."):
        value = value[part]
    return value


def actual(path):
    node = rendered
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def assert_falsy(path):
    value = locked(path)
    if value not in (False, "", [], {}, None):
        raise SystemExit(f"configuration lock must keep {path} disabled, found {value!r}")


assert_falsy("app.upload_post_enabled")
assert_falsy("app.upload_post_api_key")
assert_falsy("app.upload_post_username")
assert_falsy("app.upload_post_auto_upload")
assert_falsy("app.enable_redis")

if locked("app.upload_post_platforms") != [] or actual("app.upload_post_platforms") != []:
    raise SystemExit("upload_post_platforms must stay empty in the rendered config")
for path in (
    "app.material_directory",
    "app.video_source",
    "app.subtitle_provider",
    "app.video_codec",
    "app.endpoint",
    "listen_host",
    "listen_port",
):
    if actual(path) != locked(path):
        raise SystemExit(
            f"rendered config drifted from the lock: {path}={actual(path)!r} "
            f"(locked {locked(path)!r})"
        )

if not rendered.get("app", {}).get("api_key"):
    raise SystemExit("rendered config has no API key; refusing to start")

if rendered["listen_host"] != "127.0.0.1":
    raise SystemExit("upstream API must stay bound to loopback inside the container")
PY

log "configuration lock verified"

# --- Verify the pinned runtime inventory --------------------------------------
[ -f "${INVENTORY_FILE}" ] || fail "runtime inventory is missing: ${INVENTORY_FILE}"

PYTHONPATH="${APP_DIR}:${VERTEP_DIR}" python3 - "${INVENTORY_FILE}" <<'PY'
import sys
from pathlib import Path

from adapters.providers.base import BRIDGE_SCHEMA_VERSION, BRIDGE_VERSION
from adapters.providers.runtime_manifest import (
    PINNED_UPSTREAM_COMMIT,
    RuntimeManifestError,
    verify_inventory,
)

try:
    verify_inventory(
        sys.argv[1],
        root=Path(sys.argv[1]).parent,
        expected_commit=PINNED_UPSTREAM_COMMIT,
        expected_bridge_version=BRIDGE_VERSION,
        expected_bridge_schema_version=BRIDGE_SCHEMA_VERSION,
    )
except RuntimeManifestError as error:
    raise SystemExit(f"runtime inventory rejected: {error}")
PY

log "runtime inventory verified"

# --- Start the pinned upstream API on loopback --------------------------------
cd "${APP_DIR}"
PYTHONPATH="${APP_DIR}:${VERTEP_DIR}" python3 -m uvicorn app.asgi:app \
    --host 127.0.0.1 --port 8080 --log-level info &
UPSTREAM_PID=$!

cleanup() {
    log "stopping upstream API (pid ${UPSTREAM_PID})"
    kill "${UPSTREAM_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- Serve the Vertep wrapper -------------------------------------------------
log "starting Vertep wrapper on 0.0.0.0:${WRAPPER_PORT}"
cd "${VERTEP_DIR}"
PYTHONPATH="${APP_DIR}:${VERTEP_DIR}" exec python3 -m uvicorn services.moneyprinter_service:app \
    --host 0.0.0.0 \
    --port "${WRAPPER_PORT}" \
    --log-level info