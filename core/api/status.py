"""Status endpoints: /api/status, /status (Issue #110 Phase C)."""
from __future__ import annotations

import os
import socket

from fastapi import APIRouter

from ..models import JobStatus, StageStatus
from ..state import store, task_queue
from ..system_state import get_system_state
from ..update_manager import update_status
from ..version import application_version

router = APIRouter()


def _get_bot_username():
    try:
        from adapters.telegram import TelegramAdapter
        adapter = TelegramAdapter()
        if not adapter.configured:
            return None
        me = adapter.get_me()
        if not me.get("ok"):
            return None
        return me.get("result", {}).get("username")
    except Exception:
        return None


def _get_system_resources() -> dict | None:
    try:
        import psutil
        return {
            "cpu": int(psutil.cpu_percent(interval=0.1)),
            "ram": int(psutil.virtual_memory().percent),
            "disk": int(psutil.disk_usage('/').percent),
        }
    except Exception:
        return None


def _build_telegram_status() -> dict:
    from ..first_run import ensure_secret_store
    from ..telegram_store import load_telegram_settings
    from ..telegram_pump import service_snapshot
    secrets = ensure_secret_store()
    token_configured = bool(secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    public_url = os.getenv("PUBLIC_URL", "")
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    settings = load_telegram_settings()
    polling_enabled = os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() == "true"
    snap = service_snapshot()
    return {
        "configured": token_configured,
        "allowed_chat_ids": settings.get("allowed_chat_ids", ""),
        "admin_chat_ids": settings.get("admin_chat_ids", ""),
        "polling_enabled": polling_enabled,
        "polling_status": snap["polling_status"],
        "bot_username": snap["bot_username"],
        "last_update_id": snap["last_update_id"],
        "last_message_at": snap["last_message_at"],
        "public_url": public_url,
        "webhook_secret_configured": bool(webhook_secret),
    }


@router.get("/api/status")
def system_status():
    from .workers import workers
    from adapters.providers import provider_matrix
    from ..api.job_helpers import _job_is_due
    try:
        with socket.create_connection((os.getenv("POSTGRES_HOST", "postgres"), 5432), timeout=1):
            postgres = "OK"
    except OSError:
        postgres = "OFFLINE"
    scheduled = sorted((job.scheduled_for for job in store.jobs.values()
                        if job.status == JobStatus.NEW and job.scheduled_for and not _job_is_due(job)))
    scheduled_next = scheduled[0] if scheduled else None
    queue_info = {"backend": task_queue.backend, "depth": task_queue.depth(),
                  "inflight": task_queue.inflight_depth(),
                  "dead_letter": len(task_queue.dead_letters())}
    orchestration = {"active_jobs": sum(job.status not in {JobStatus.READY, JobStatus.PUBLISHED,
                                                           JobStatus.FAILED, JobStatus.CANCELLED}
                                        for job in store.jobs.values()),
                     "active_scenes": sum(scene.status == StageStatus.RUNNING
                                          for job in store.jobs.values() for scene in job.scenes)}
    return {"core": "OK", "version": application_version(), "system": get_system_state(),
            "storage": "OK", "redis": "OK" if task_queue.backend == "redis" else "OFFLINE",
            "postgres": postgres,
            "queue": queue_info,
            "scheduler": {"pending": len(scheduled), "next_run": scheduled_next},
            "orchestration": orchestration,
            "ollama": "STUB" if os.getenv("DEMO_MODE", "true").lower() == "true" else "CONFIGURED",
            "telegram": _build_telegram_status(),
            "providers": provider_matrix(),
            "resources": _get_system_resources(),
            "update": update_status(), "workers": workers()}

@router.get("/status", include_in_schema=False)
def status_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse("<!DOCTYPE html><html><body>Vertep Status</body></html>")

