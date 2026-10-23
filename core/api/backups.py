"""System API backup/restore routes (Issue #110 Phase E).

Extracted verbatim from ``core/app.py``: the same paths, methods and response
codes.  The actual logic calls the Backup Service via :func:`_internal_api`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..api._internal import _internal_api

router = APIRouter()


@router.get("/api/system/backups")
async def system_backups():
    return await _internal_api("GET", "BACKUP_URL", "/snapshots")


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
        "GET", "BACKUP_URL", f"/snapshots/{snapshot_id}/restore/progress"
    )


@router.post("/api/system/backups/{snapshot_id}/restore")
async def restore_system_backup(snapshot_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Invalid snapshot identifier")
    return await _internal_api(
        "POST", "BACKUP_URL", f"/snapshots/{snapshot_id}/restore"
    )


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
    )
