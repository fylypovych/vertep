# This script provides a minimal interface for managing rollback operations.
# It is meant to be used by the worker when it needs to revert to a previous
# provider configuration in case of a failed activation.
#
# The implementation relies on the `request_local_update` helper defined in
# `worker/service.py`.  When a rollback is requested, the worker should call this
# function with the desired target version.  If the target is "previous", it
# delegates to the last successful version that the Core has recorded.
#
# NOTE: The actual logic for retrieving the previous version and persisting
# the rollback state is intentionally left as a TODO, because the repository
# currently doesn’t have a stored history of provider configurations.  The
# implementation below provides a basic framework that can be extended later.

import os
import json
from pathlib import Path
import secrets

# The directory where the worker writes rollback markers.  This must match
# the `UPDATE_REQUEST_DIR` used in `worker/service.py`.
UPDATE_DIR = Path(os.getenv("UPDATE_REQUEST_DIR", ""))


def request_rollback(target_version: str | None = None, request_id: str | None = None) -> None:
    """Trigger a rollback update.

    Parameters
    ----------
    target_version:
        The version to revert to.  If *None*, the worker should determine the
        previous successful deployment.  This placeholder simply uses the
        string ``"previous"``.
    request_id:
        Optional id for the request.  If omitted, a random 32‑hex value is
        generated.
    """
    if target_version is None:
        target_version = "previous"
    # Use the same marker logic as `request_local_update`.
    marker_value = f"rollback:{target_version}"
    if request_id:
        marker_value += f":{request_id}"
    else:
        request_id = secrets.token_hex(16)
    if not UPDATE_DIR.exists():
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    marker = UPDATE_DIR.parent / "worker-update-target"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == marker_value:
        return
    temp = UPDATE_DIR / f".{request_id}.tmp"
    temp.write_text(json.dumps({"request_id": request_id, "action": "rollback", "target_version": target_version}), encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(UPDATE_DIR / f"{request_id}.json")
    marker.write_text(marker_value, encoding="utf-8")

# A helper that can be imported by the worker process.
__all__ = ["request_rollback"]
