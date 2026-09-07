import json
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response
from ..models import JobStatus, StageStatus, WorkerLogBatch, utc_now
from ..health_checks import run_checks as _run_health_checks, health_status as _health_status
from ..logging_config import read_logs
from ..maintenance import cleanup_jobs, cleanup_temporary_files
from ..security import _valid_worker_request
from ..state import store, task_queue, logger
from ..first_run import config_root
from ..system_state import get_system_state
from ..update_manager import update_status
from .job_helpers import _job_is_due
from .workers import workers

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    checks = _run_health_checks("core")
    return {"status": _health_status(checks), "service": "core", "jobs": len(store.jobs), "checks": checks}



@router.post("/api/watchdog/report")
def watchdog_report(request: Request):
    if not _valid_worker_request(request.headers.get("x-vertep-node-name", ""), request):
        raise HTTPException(401, "Invalid worker token")
    try:
        payload = request.json()
    except Exception:
        payload = {}
    report = {"received_at": utc_now().isoformat(), "payload": payload}
    report_path = Path(os.getenv("UPDATE_STATE_DIR", "/data/config/update")) / "watchdog-reports.jsonl"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False) + "\n")
    return {"accepted": True}



@router.get("/api/health/history")
def health_history(limit: int = 100):
    history_path = Path(os.getenv("UPDATE_STATE_DIR", "/data/config/update")) / "health-history.jsonl"
    if not history_path.exists():
        return {"history": []}
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()[-limit:]
        return {"history": [json.loads(line) for line in lines if line.strip()]}
    except (OSError, ValueError):
        return {"history": []}



@router.get("/api/security/check")
def security_check():
    values = {"ADMIN_PASSWORD": os.getenv("ADMIN_PASSWORD", ""), "NODE_API_TOKEN": os.getenv("NODE_API_TOKEN", ""),
              "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD", "")}
    weak = [key for key, value in values.items() if len(value) < 16 or "replace" in value.lower()]
    return {"ok": not weak, "weak_or_missing": weak, "recommendation": "Use random values of at least 32 characters"}



@router.get("/api/logs")
def logs(limit: int = 200, level: str | None = None, job_id: str | None = None, node_name: str | None = None):
    return read_logs(min(max(limit, 1), 1000), level, job_id, node_name)



@router.post("/api/logs/ingest")
def ingest_logs(batch: WorkerLogBatch, request: Request):
    if not _valid_worker_request(batch.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    for entry in batch.entries:
        level = str(entry.get("level", "INFO")).upper()
        logger.log(getattr(__import__("logging"), level, 20), str(entry.get("message", "")),
                   extra={"node_name": batch.node_name, "job_id": entry.get("job_id")})
    return {"accepted": len(batch.entries)}



@router.get("/api/metrics")
def metrics():
    statuses = {status.value: sum(job.status == status for job in store.jobs.values()) for status in JobStatus}
    scene_statuses = {status.value: sum(scene.status == status for job in store.jobs.values() for scene in job.scenes)
                      for status in StageStatus}
    return {"jobs_total": len(store.jobs), "jobs_by_status": statuses, "queue_ready": task_queue.depth(),
            "queue_inflight": task_queue.inflight_depth(),
            "queue_dead_letter": len(task_queue.dead_letters()),
            "jobs_scheduled": sum(job.status == JobStatus.NEW and not _job_is_due(job) for job in store.jobs.values()),
            "scenes_by_status": scene_statuses,
            "workers_online": sum(worker.get("status") != "OFFLINE" for worker in store.workers.values())}



@router.get("/metrics", response_class=Response)
def prometheus_metrics():
    values = metrics()
    lines = [f"vertep_jobs_total {values['jobs_total']}", f"vertep_queue_ready {values['queue_ready']}",
             f"vertep_queue_inflight {values['queue_inflight']}",
             f"vertep_queue_dead_letter {values['queue_dead_letter']}",
             f"vertep_jobs_scheduled {values['jobs_scheduled']}",
             f"vertep_workers_online {values['workers_online']}"]
    lines.extend(f'vertep_jobs_status{{status="{status}"}} {count}' for status, count in values["jobs_by_status"].items())
    lines.extend(f'vertep_scenes_status{{status="{status}"}} {count}' for status, count in values["scenes_by_status"].items())
    return Response("\n".join(lines) + "\n", media_type="text/plain")



@router.get("/api/alerts")
def alerts():
    result = []
    system = get_system_state()
    if system.get("state") != "NORMAL":
        result.append({"severity": "error" if system.get("state") == "EMERGENCY" else "warning",
                       "type": "SYSTEM_STATE", "state": system.get("state"),
                       "message": system.get("reason") or "System is not in normal mode",
                       "operation_id": system.get("operation_id"),
                       "updated_at": system.get("updated_at")})
    update = update_status()
    if update.get("state") in {"FAILED", "ROLLED_BACK"}:
        result.append({"severity": "error", "type": "UPDATE_FAILED",
                       "message": update.get("message") or "Update failed",
                       "operation_id": update.get("request_id"),
                       "updated_at": update.get("updated_at"),
                       "details": (update.get("log") or [])[-20:]})
    deployment = _read_optional_json(config_root() / "deployment-status.json")
    if deployment.get("state") == "FAILED":
        result.append({"severity": "error", "type": "ROLE_DEPLOYMENT_FAILED",
                       "message": deployment.get("error") or "Role activation failed",
                       "updated_at": deployment.get("updated_at")})
    for job in store.jobs.values():
        if job.status == JobStatus.FAILED:
            result.append({"severity": "error", "type": "JOB_FAILED", "job_id": job.job_id,
                           "message": job.events[-1] if job.events else "Job failed"})
    for worker in workers():
        if worker.get("status") in {"OFFLINE", "ERROR"}:
            result.append({"severity": "error", "type": "WORKER_OFFLINE", "node_name": worker["node_name"]})
    for task in task_queue.dead_letters():
        result.append({"severity": "error", "type": "DEAD_LETTER_TASK", "job_id": task.get("job_id"),
                       "task_id": task.get("task_id"), "message": task.get("error") or "Task retries exhausted"})
    return result[-200:]



@router.post("/api/maintenance/cleanup")
def maintenance_cleanup(dry_run: bool = True, retention_days: int | None = None):
    result = cleanup_jobs(store, retention_days, dry_run)
    result["temporary_files"] = cleanup_temporary_files(store.root, dry_run=dry_run)
    logger.info("Maintenance cleanup", extra={"action": "dry-run" if dry_run else "delete"})
    return result



def _read_optional_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
