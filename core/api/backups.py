"""System API backup/restore routes (Issue #110 Phase E).

Extracted verbatim from ``core/app.py``: the same paths, methods and response
codes.  The actual logic calls the Backup Service via :func:`_internal_api`.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..api._internal import _internal_api
from ..system_state import get_system_state, set_system_state, SystemState
from ..state import store, task_queue

router = APIRouter()


@router.get("/api/system/backups")
async def system_backups():
    return await _internal_api("GET", "BACKUP_URL", "/snapshots")


@router.get("/api/system/backups/{snapshot_id}/download")
def download_system_backup(snapshot_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(400, "Invalid snapshot id")
    base = os.getenv("BACKUP_URL", "").rstrip("/")
    if not base:
        raise HTTPException(503, "Backup service is not configured")
    client = httpx.Client(timeout=httpx.Timeout(30, read=600))
    upstream = client.send(client.build_request("GET", f"{base}/snapshots/{snapshot_id}/download"), stream=True)
    if upstream.status_code != 200:
        status = upstream.status_code
        upstream.close()
        client.close()
        raise HTTPException(status, "Backup is not available")

    def body():
        try:
            yield from upstream.iter_bytes(1024 * 1024)
        finally:
            upstream.close()
            client.close()

    headers = {name: upstream.headers[name] for name in ("content-disposition", "content-length")
               if name in upstream.headers}
    return StreamingResponse(body(), media_type="application/octet-stream", headers=headers)


@router.post("/api/system/backups")
async def create_system_backup():
    return await _internal_api(
        "POST",
        "BACKUP_URL",
        "/snapshots",
        {
            "job_id": "system-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "request": {},
        },
    )


@router.get("/api/system/backups/{snapshot_id}/restore/progress")
async def restore_progress_proxy(snapshot_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Invalid snapshot identifier")
    return await _internal_api(
        "GET",
        "BACKUP_URL",
        f"/snapshots/{snapshot_id}/restore/progress",
        propagate_client_errors=True,
    )


@router.post("/api/system/backups/{snapshot_id}/restore")
async def restore_system_backup(snapshot_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Invalid snapshot identifier")
    state = get_system_state()
    owner = str(state.get("operation_id") or "")
    if owner.startswith("restore:") and owner != f"restore:{snapshot_id}":
        raise HTTPException(409, "Another restore is awaiting confirmation or executing")
    safe_jobs = {"PAUSED", "FAILED", "CANCELLED", "PUBLISHED", "READY", "SCRIPT_PENDING_APPROVAL",
                 "STORYBOARD_PENDING_APPROVAL", "STORYBOARD_READY", "VIDEO_PENDING_APPROVAL",
                 "VIDEO_REVISION_REQUESTED", "WAITING_FOR_SYSTEM"}
    if task_queue.inflight_depth() or any(job.status.value not in safe_jobs for job in list(store.jobs.values())):
        raise HTTPException(409, "Active jobs must be drained before restore")
    # The owner already requested restore; freeze writes before obtaining its
    # snapshot-bound challenge. Cancellation/failure must not imply NORMAL.
    previous = dict(state)
    operation_id = f"restore:{snapshot_id}"
    set_system_state(SystemState.MAINTENANCE, f"Restore confirmation for {snapshot_id}", operation_id)
    try:
        return await _internal_api(
            "POST",
            "BACKUP_URL",
            f"/snapshots/{snapshot_id}/restore",
            propagate_client_errors=True,
        )
    except HTTPException as error:
        # A rejection of the challenge itself did not start a restore: release
        # the durable fence so the caller's refusal leaves the state untouched.
        if 400 <= int(error.status_code) < 500:
            current = get_system_state()
            if str(current.get("operation_id") or "") == operation_id:
                set_system_state(
                    previous["state"],
                    previous.get("reason") or "",
                    previous.get("operation_id"),
                )
        raise


@router.post("/api/system/backups/{snapshot_id}/restore/confirm")
async def confirm_system_backup(snapshot_id: str, token: str):
    """Issue #137: complete the two-phase restore started by /restore.

    The Backup Service returns a single-use confirmation token from /restore;
    CORE forwards it here so Web/Telegram can execute the destructive restore
    only after an explicit second acknowledgement.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Invalid snapshot identifier")
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise HTTPException(422, "Invalid confirmation token")
    return await _internal_api(
        "POST",
        "BACKUP_URL",
        f"/snapshots/{snapshot_id}/restore/confirm",
        params={"token": token},
        propagate_client_errors=True,
    )
