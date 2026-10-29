from __future__ import annotations

from adapters.providers import provider_matrix
from core.api.job_helpers import _job_is_due
from core.api.workers import workers
from core.models import JobStatus
from core.models import StageStatus
from core.state import store
from core.state import task_queue
from core.system_state import get_system_state
from core.update_manager import update_status
from core.version import application_version
import os
import psutil
import secrets
import socket
from fastapi.responses import HTMLResponse
from fastapi import APIRouter
router = APIRouter()
from core.telegram import handlers
from core.api.telegram import _get_bot_username

def _build_telegram_status() -> dict:
    from core.first_run import ensure_secret_store
    secrets = ensure_secret_store()
    token_configured = bool(secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    if not token_configured:
        return {"enabled": False, "mode": "polling", "status": "not_configured"}
    polling_enabled = os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() == "true"
    status = "running" if handlers.telegram_polling_service and handlers.telegram_polling_service.running else "stopped"
    if handlers.telegram_polling_service and handlers.telegram_polling_service.last_error:
        status = "error"
    return {
        "enabled": True,
        "mode": "polling" if polling_enabled else "disabled",
        "status": status,
        "bot_username": _get_bot_username(),
        "last_update_id": handlers.telegram_polling_service.last_update_id if handlers.telegram_polling_service else None,
        "last_message_at": handlers.telegram_polling_service.last_message_at if handlers.telegram_polling_service else None,
        "last_error": handlers.telegram_polling_service.last_error if handlers.telegram_polling_service else None,
        "consecutive_failures": handlers.telegram_polling_service._consecutive_failures if handlers.telegram_polling_service else 0,
    }


def _get_system_resources() -> dict | None:
    try:
        return {
            "cpu": int(psutil.cpu_percent(interval=0.1)),
            "ram": int(psutil.virtual_memory().percent),
            "disk": int(psutil.disk_usage('/').percent),
        }
    except Exception:
        return None


@router.get("/api/status")
def system_status():
    try:
        with socket.create_connection((os.getenv("POSTGRES_HOST", "postgres"), 5432), timeout=1):
            postgres = "OK"
    except OSError:
        postgres = "OFFLINE"
    scheduled = sorted((job.scheduled_for for job in store.jobs.values()
                        if job.status == JobStatus.NEW and job.scheduled_for and not _job_is_due(job)))
    return {"core": "OK", "version": application_version(), "system": get_system_state(), "storage": "OK", "redis": "OK" if task_queue.backend == "redis" else "OFFLINE",
            "postgres": postgres,
            "queue": {"backend": task_queue.backend, "depth": task_queue.depth(),
                      "inflight": task_queue.inflight_depth(),
                      "dead_letter": len(task_queue.dead_letters())},
            "scheduler": {"pending": len(scheduled), "next_run": scheduled[0] if scheduled else None},
            "orchestration": {"active_jobs": sum(job.status not in {JobStatus.READY, JobStatus.PUBLISHED,
                                                                       JobStatus.FAILED, JobStatus.CANCELLED}
                                                  for job in store.jobs.values()),
                              "active_scenes": sum(scene.status == StageStatus.RUNNING
                                                   for job in store.jobs.values() for scene in job.scenes)},
             "ollama": "STUB" if os.getenv("DEMO_MODE", "true").lower() == "true" else "CONFIGURED",
             "telegram": _build_telegram_status(),
             "providers": provider_matrix(),
             "resources": _get_system_resources(),
             "update": update_status(), "workers": workers()}


@router.get("/status")
def status_page():
    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
  <title>Vertep Status</title>
  <meta charset="utf-8">
  <style>
    body { font-family: sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }
    h1 { font-size: 1.5rem; margin-bottom: 1rem; }
    pre { background: #1e293b; padding: 1rem; border-radius: 0.5rem; overflow-x: auto; }
    .ok { color: #4ade80; } .off { color: #f87171; } .warn { color: #facc15; }
  </style>
</head>
<body>
  <h1>Vertep Status</h1>
  <pre id="status">Loading...</pre>
  <script>
    async function load() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        const el = document.getElementById('status');
        let text = JSON.stringify(data, null, 2);
        text = text.replace(/"OK"/g, '<span class="ok">"OK"</span>');
        text = text.replace(/"OFFLINE"/g, '<span class="off">"OFFLINE"</span>');
        text = text.replace(/"STUB"/g, '<span class="warn">"STUB"</span>');
        text = text.replace(/"NOT CONFIGURED"/g, '<span class="off">"NOT CONFIGURED"</span>');
        el.innerHTML = text;
      } catch (e) {
        document.getElementById('status').textContent = 'Failed to load status: ' + e;
      }
    }
    load();
    setInterval(load, 5000);
  </script>
</body>
</html>""")

