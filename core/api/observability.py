import hashlib
import json
import os
from pathlib import Path
from fastapi import APIRouter, Body, HTTPException, Request
from starlette.responses import Response
from ..models import JobStatus, StageStatus, WorkerLogBatch, utc_now
from ..health_checks import run_checks as _run_health_checks, health_status as _health_status
from ..logging_config import read_logs, secret_redact
from ..maintenance import cleanup_jobs, cleanup_temporary_files
from ..security import _valid_worker_request
from ..state import store, task_queue, logger
from ..first_run import config_root, default_update_state_dir
from ..alert_store import get_alert_store
from ..system_state import get_system_state
from ..update_manager import update_status
from ..runtime_identity import CORE_RUNTIME_INSTANCE_ID
from .job_helpers import _job_is_due
from .workers import workers

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    checks = _run_health_checks("core")
    return {"status": _health_status(checks), "service": "core",
            "runtime_instance_id": CORE_RUNTIME_INSTANCE_ID,
            "jobs": len(store.jobs), "checks": checks}


@router.get("/api/health/ready")
def health_ready():
    """Readiness gate for container/probe health checks.

    Unlike ``/api/health`` (a liveness/info endpoint that always answers 200),
    this probe returns HTTP 503 whenever the aggregated status is UNHEALTHY so
    that a successful ``curl`` on the health gate never implies a healthy node.
    """
    checks = _run_health_checks("core")
    status = _health_status(checks)
    payload = {"status": status, "service": "core",
               "runtime_instance_id": CORE_RUNTIME_INSTANCE_ID,
               "jobs": len(store.jobs), "checks": checks}
    response = Response(json.dumps(payload, ensure_ascii=False), media_type="application/json")
    if status != "HEALTHY":
        response.status_code = 503
    return response



@router.post("/api/watchdog/report")
async def watchdog_report(request: Request):
    if not _valid_worker_request(request.headers.get("x-vertep-node-name", ""), request):
        raise HTTPException(401, "Invalid worker token")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    report = {"received_at": utc_now(), "payload": payload}
    report_path = default_update_state_dir() / "watchdog-reports.jsonl"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False) + "\n")
    return {"accepted": True}



@router.get("/api/health/history")
def health_history(limit: int = 100):
    history_path = default_update_state_dir() / "health-history.jsonl"
    if not history_path.exists():
        return {"history": []}
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()[-limit:]
        return {"history": [json.loads(line) for line in lines if line.strip()]}
    except (OSError, ValueError):
        return {"history": []}



@router.get("/api/security/check")
def security_check():
    weak = _env_weak_values()
    seals = _secret_store_status()
    certificates = _certificate_statuses()
    integrations = _integration_summary()

    remediation = [f"Set a strong, unique random value for {name}" for name in weak]
    if seals.get("sealed") is False:
        remediation.append("Re-seal the encrypted secret-store data key with SECRET_STORE_PASSPHRASE")
    if seals.get("status") == "unusable":
        remediation.append(
            "The sealed secret-store data key cannot be opened with the configured "
            f"passphrase ({seals.get('detail')}); restore the matching SECRET_STORE_PASSPHRASE")
    for label, info in certificates.items():
        if info.get("status") in ("expiring", "expired"):
            remediation.append(f"{label} is {info['status']} (expires {info.get('expires_at')}); renew it")
        elif info.get("status") == "missing":
            remediation.append(f"{label} is missing; provision it")
        elif info.get("status") == "unreadable":
            remediation.append(f"{label} is present but unreadable; restore or replace it")

    # Fail-closed: a missing or unreadable certificate is never reported as ok.
    # Previously a missing cert was folded into the "ok" bucket alongside a
    # healthy one, so a deployment without TLS could still pass the gate.
    certs_ok = all(info.get("status") == "ok" for info in certificates.values())
    ok = (not weak) and seals.get("status") == "ok" and certs_ok
    return {
        "ok": ok,
        "weak_or_missing": list(weak),
        "recommendation": (" ; ".join(remediation) or "Environment credentials and certificates are within policy"),
        "checks": {"secrets_store": seals, "certificates": certificates, "integrations": integrations},
    }


_WEAK_VALUE_MARKERS = ("replace", "changeme", "change-me", "example", "<your", "todo", "test")


def _env_weak_values() -> dict[str, str]:
    fields = ("ADMIN_PASSWORD", "NODE_API_TOKEN", "POSTGRES_PASSWORD",
              "SECRET_STORE_PASSPHRASE", "SESSION_SECRET")
    weak: dict[str, str] = {}
    for name in fields:
        value = os.getenv(name, "")
        lowered = value.lower()
        if not value or len(value) < 16 or any(marker in lowered for marker in _WEAK_VALUE_MARKERS):
            weak[name] = value
    return weak


def _secret_store_status() -> dict:
    from ..first_run import inspect_data_key
    state = inspect_data_key()
    sealed = state.get("sealed")
    passphrase_configured = bool(os.getenv("SECRET_STORE_PASSPHRASE") or os.getenv("SECRET_STORE_PASSPHRASE_FILE"))
    sealing_required = os.getenv("REQUIRE_SECRET_KEY_SEALING", "false").lower() == "true"
    if sealed is True and state.get("unsealable") is False:
        # Fail-closed: a sealed key the Core cannot open breaks every secret.
        status = "unusable"
    elif sealed is False and (passphrase_configured or sealing_required):
        status = "warning"
    else:
        status = "ok"
    return {"status": status, "sealed": sealed, "detail": state.get("detail"),
            "unsealable": state.get("unsealable"),
            "passphrase_configured": passphrase_configured, "sealing_required": sealing_required}


def _certificate_statuses() -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    certificates: dict = {}
    for label, env_name, default in (("server_certificate", "CORE_CERTIFICATE_PATH", "/data/tls/vertep.crt"),
                                     ("server_key", "CORE_KEY_PATH", "/data/tls/vertep.key"),
                                     ("node_ca", "NODE_CA_CERT_PATH", "/data/tls/node-ca.crt")):
        path = Path(os.getenv(env_name, default))
        info = {"present": path.exists()}
        if not path.exists():
            info["status"] = "missing"
            certificates[label] = info
            continue
        try:
            raw = path.read_bytes()
            info["sha256"] = hashlib.sha256(raw).hexdigest()
            info["size_bytes"] = len(raw)
            if label.endswith("_key"):
                info["status"] = "ok"
                try:
                    serialization.load_pem_private_key(raw, password=None)
                except Exception:
                    info["status"] = "unreadable"
            else:
                cert = x509.load_pem_x509_certificate(raw)
                not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
                days = (not_after - now).days
                info.update({
                    "status": "ok" if days > 7 else ("expiring" if days > 0 else "expired"),
                    "expires_at": not_after.isoformat(),
                    "days_remaining": days,
                    "subject": cert.subject.rfc4514_string(),
                })
        except Exception:
            info["status"] = "unreadable"
        certificates[label] = info
    return certificates


def _integration_summary() -> list[dict]:
    items = []
    if os.getenv("PUBLISHER_MOCK", "").lower() == "true":
        items.append({"name": "publisher", "status": "mock"})
    if os.getenv("VERTEP_LLM_PROVIDER") == "openai":
        items.append({"name": "llm", "status": "external"})
    # Per-platform publisher credential readiness
    try:
        from adapters.publisher import PUBLISHERS
        for channel, pub in PUBLISHERS.items():
            items.append({
                "name": f"publisher:{channel}",
                "status": "ready" if pub.ready() else ("configured" if pub.configured() else "not_configured"),
                "missing_scopes": pub.missing_scopes(),
            })
    except Exception:
        pass
    return items



@router.get("/api/logs")
def logs(limit: int = 200, level: str | None = None, job_id: str | None = None,
         node_name: str | None = None, before: str | None = None):
    return read_logs(min(max(limit, 1), 1000), level, job_id, node_name, before)



@router.post("/api/logs/ingest")
def ingest_logs(batch: WorkerLogBatch, request: Request):
    if not _valid_worker_request(batch.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    for entry in batch.entries:
        level = str(entry.get("level", "INFO")).upper()
        # Redact secrets the moment untrusted worker text crosses the boundary;
        # JsonFormatter additionally scrubs on serialisation.
        logger.log(getattr(__import__("logging"), level, 20), secret_redact(str(entry.get("message", ""))),
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
def alerts(limit: int = 200, state: str | None = None):
    _reconcile_alerts()
    return get_alert_store().list(limit=min(max(limit, 1), 1000), state=state)


@router.post("/api/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: str, actor: str = Body("", embed=True)):
    try:
        return get_alert_store().acknowledge(alert_id, actor or "")
    except KeyError:
        raise HTTPException(404, "Alert not found")
    except ValueError as error:
        raise HTTPException(409, str(error))


def _reconcile_alerts() -> None:
    """Fold the current system/update/deployment/job/worker/task state into the
    persistent alert store, recording firing alerts (deduplicated) and resolving
    those that recovered -- without erasing history."""
    alert_svc = get_alert_store()

    system = get_system_state()
    if system.get("state") != "NORMAL":
        alert_svc.record({
            "severity": "error" if system.get("state") == "EMERGENCY" else "warning",
            "type": "SYSTEM_STATE",
            "state_value": system.get("state"),
            "message": system.get("reason") or "System is not in normal mode",
            "operation_id": system.get("operation_id"),
            "updated_at": system.get("updated_at"),
        })
    else:
        alert_svc.resolve({"type": "SYSTEM_STATE"}, "system returned to NORMAL")

    update = update_status()
    if update.get("state") in {"FAILED", "ROLLED_BACK"}:
        alert_svc.record({
            "severity": "error", "type": "UPDATE_FAILED",
            "message": update.get("message") or "Update failed",
            "operation_id": update.get("request_id"),
            "updated_at": update.get("updated_at"),
            "details": (update.get("log") or [])[-20:],
        })
    else:
        alert_svc.resolve({"type": "UPDATE_FAILED"}, "update operation finished")

    deployment = _read_optional_json(config_root() / "deployment-status.json")
    if deployment.get("state") == "FAILED":
        alert_svc.record({
            "severity": "error", "type": "ROLE_DEPLOYMENT_FAILED",
            "message": deployment.get("error") or "Role activation failed",
            "updated_at": deployment.get("updated_at"),
        })
    else:
        alert_svc.resolve({"type": "ROLE_DEPLOYMENT_FAILED"}, "deployment recovered")

    failed_jobs = {job.job_id for job in store.jobs.values() if job.status == JobStatus.FAILED}
    for job_id in failed_jobs:
        job = store.jobs.get(job_id)
        alert_svc.record({
            "severity": "error", "type": "JOB_FAILED", "job_id": job_id,
            "message": job.events[-1] if job and job.events else "Job failed",
        })
    for alert in alert_svc.list():
        if alert.get("type") == "JOB_FAILED" and alert.get("job_id") and alert["job_id"] not in failed_jobs:
            alert_svc.resolve({"type": "JOB_FAILED", "job_id": alert["job_id"]}, "job recovered")

    online_names = {w.get("node_name") for w in workers() if w.get("status") in ("ONLINE", "FREE", "BUSY")}
    for worker in workers():
        if worker.get("status") in {"OFFLINE", "ERROR"}:
            alert_svc.record({
                "severity": "error", "type": "WORKER_OFFLINE", "node_name": worker["node_name"],
            })
    for alert in alert_svc.list():
        if alert.get("type") == "WORKER_OFFLINE" and alert.get("node_name") and alert["node_name"] in online_names:
            alert_svc.resolve({"type": "WORKER_OFFLINE", "node_name": alert["node_name"]}, "node is back online")

    dead = task_queue.dead_letters()
    dead_keys = {(item.get("job_id"), item.get("task_id")) for item in dead}
    for item in dead:
        alert_svc.record({
            "severity": "error", "type": "DEAD_LETTER_TASK",
            "job_id": item.get("job_id"), "task_id": item.get("task_id"),
            "message": item.get("error") or "Task retries exhausted",
        })
    for alert in alert_svc.list():
        if alert.get("type") == "DEAD_LETTER_TASK" and \
                (alert.get("job_id"), alert.get("task_id")) not in dead_keys:
            alert_svc.resolve({"type": "DEAD_LETTER_TASK",
                               "job_id": alert.get("job_id"), "task_id": alert.get("task_id")},
                              "dead-letter task drained")



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
