"""Issue #555: post-restore readiness waits for fresh Worker heartbeat instead of
failing into EMERGENCY on an expired heartbeat restored from an older snapshot."""

from __future__ import annotations

import base64
import concurrent.futures
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from core import health_checks
from core.app import app as core_app
from core.repository import FileRepository
from core.state import store
from core.system_state import SystemState, set_system_state
from services import backup_service


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    pytest.fail("Timed out waiting for condition")


@pytest.fixture
def restore_heartbeat_env(monkeypatch, tmp_path):
    """Real CORE app over local HTTP + real Backup Service with real worker health check.

    External infrastructure probes (docker/postgres/redis/ollama/backup) are stubbed
    to healthy by default so the test exercises real AdminAuthMiddleware, real
    /api/health, real /api/workers/heartbeat, and unpatched check_local_worker().
    """
    config = tmp_path / "config"
    storage = tmp_path / "storage"
    backups = tmp_path / "backups"
    update_dir = tmp_path / "update-state"
    restore_state = tmp_path / "restore-state"
    for directory in (config, storage, backups, update_dir, restore_state):
        directory.mkdir(parents=True, exist_ok=True)

    (config / "users.json").write_text("{}", encoding="utf-8")
    (config / "installation.json").write_text(
        json.dumps({"completed_at": "2026-01-01T00:00:00Z", "node_role": "core",
                    "ai_backend": {"type": "ollama"}}),
        encoding="utf-8",
    )
    (config / "deployment-plan.json").write_text(
        json.dumps({"role": "core", "additional_roles": ["text", "backup"],
                    "services": ["core", "ollama", "worker", "backup-service"]}),
        encoding="utf-8",
    )

    internal_key = "test-555-internal-key"
    worker_token = "test-555-worker-token"
    admin_password = "test-555-admin-password"
    node_name = "local-worker"

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        core_port = probe.getsockname()[1]
    core_url = f"http://127.0.0.1:{core_port}"

    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(update_dir))
    monkeypatch.setenv("STORAGE_ROOT", str(storage))
    monkeypatch.setenv("JOB_ROOT", str(storage))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    monkeypatch.setenv("NODE_NAME", node_name)
    monkeypatch.setenv("HEARTBEAT_TIMEOUT", "45")
    monkeypatch.setenv("NODE_MTLS_REQUIRED", "false")
    monkeypatch.setenv("NODE_API_TOKEN", worker_token)
    monkeypatch.setenv("INTERNAL_API_KEY", internal_key)
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", admin_password)
    monkeypatch.setenv("USERS_JSON", "")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CORE_ADDRESS", "")
    monkeypatch.setenv("BACKUP_CORE_URL", core_url)
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_SOURCES", f"storage:{storage}")
    monkeypatch.setenv("BACKUP_RESTORE_STATE_DIR", str(restore_state))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"5" * 32).decode("ascii"))
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")
    monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
    monkeypatch.setenv("BACKUP_POST_RESTORE_HEALTH_TIMEOUT", "10")
    monkeypatch.setenv("BACKUP_POST_RESTORE_HEALTH_POLL_INTERVAL", "0.05")

    # Point CORE's store repository at the backed-up storage dir so .workers.json
    # is included in snapshots and overwritten when restoring older snapshots.
    original_repo = store.repository
    original_root = store.root
    original_workers = dict(store.workers)
    store.repository = FileRepository(storage)
    store.root = storage
    store.workers.clear()

    # Keep check_local_worker completely real; only stub external infrastructure probes.
    monkeypatch.setattr(health_checks, "check_docker", lambda: (True, "ok"))
    monkeypatch.setattr(health_checks, "check_postgres", lambda *a, **k: (True, "SELECT 1 = 1"))
    monkeypatch.setattr(health_checks, "check_redis", lambda *a, **k: (True, "PING = PONG"))
    monkeypatch.setattr(health_checks, "check_ollama", lambda *a, **k: (True, "HTTP 200"))
    monkeypatch.setattr(health_checks, "check_backup", lambda *a, **k: (True, f"writable: {backups}"))

    backup_service._restore_progress.clear()
    backup_service._restore_confirmations.clear()
    set_system_state(SystemState.NORMAL, "test-555 init", None)

    server = uvicorn.Server(uvicorn.Config(
        core_app, host="127.0.0.1", port=core_port, log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    auth = "Basic " + base64.b64encode(f"admin:{admin_password}".encode("utf-8")).decode("ascii")
    core_http = httpx.Client(base_url=core_url, timeout=15, headers={"Authorization": auth})
    try:
        _wait_for(lambda: _probe_core(core_http))
        yield {
            "core_url": core_url,
            "core_http": core_http,
            "backup_client": TestClient(backup_service.app),
            "storage": storage,
            "config": config,
            "restore_state": restore_state,
            "node_name": node_name,
            "worker_token": worker_token,
            "internal_key": internal_key,
        }
    finally:
        core_http.close()
        server.should_exit = True
        thread.join(timeout=10)
        store.repository = original_repo
        store.root = original_root
        store.workers.clear()
        store.workers.update(original_workers)
        backup_service._restore_progress.clear()
        backup_service._restore_confirmations.clear()


def _probe_core(client: httpx.Client) -> bool:
    try:
        return client.get("/api/health").status_code == 200
    except httpx.HTTPError:
        return False


def _send_heartbeat(env: dict, status: str = "READY") -> httpx.Response:
    return env["core_http"].post(
        "/api/workers/heartbeat",
        headers={"x-vertep-token": env["worker_token"]},
        json={
            "node_name": env["node_name"],
            "role": "text",
            "status": status,
            "capabilities": ["script_generation"],
        },
    )


def _write_worker_record(storage: Path, node_name: str, last_seen: datetime) -> None:
    record = {
        node_name: {
            "node_name": node_name,
            "role": "text",
            "status": "READY",
            "capabilities": ["script_generation"],
            "last_seen": last_seen.isoformat(),
        }
    }
    (storage / ".workers.json").write_text(json.dumps(record), encoding="utf-8")


def _begin_restore(env: dict, snapshot_id: str) -> str:
    # Enter MAINTENANCE with restore fence (same contract as CORE's /api/system/backups/{id}/restore).
    resp = env["core_http"].post(
        "/api/system/state",
        headers={"x-vertep-internal-key": env["internal_key"]},
        json={
            "state": "MAINTENANCE",
            "reason": f"Restore confirmation for {snapshot_id}",
            "operation_id": f"restore:{snapshot_id}",
        },
    )
    assert resp.status_code == 200, resp.text
    challenge = env["backup_client"].post(f"/snapshots/{snapshot_id}/restore")
    assert challenge.status_code == 200, challenge.text
    return challenge.json()["confirmation_token"]


def test_ac1_restore_older_snapshot_with_expired_heartbeat_recovers_after_fresh_heartbeat(restore_heartbeat_env):
    """AC1: restoring an older snapshot restores an expired Worker heartbeat; CORE stays
    UNHEALTHY until a fresh Worker heartbeat arrives during RECOVERING, then enters NORMAL."""
    env = restore_heartbeat_env
    storage = env["storage"]
    data_file = storage / "record.txt"
    data_file.write_text("older-snapshot-data", encoding="utf-8")

    # Simulate an older snapshot whose persisted Worker heartbeat is already expired (> 45s).
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    _write_worker_record(storage, env["node_name"], expired_at)
    snap = env["backup_client"].post("/snapshots", json={"job_id": "older-snap"}).json()
    sid = snap["snapshot_id"]

    # Mutate live data and register a fresh live heartbeat so CORE is HEALTHY before restore.
    data_file.write_text("newer-live-data", encoding="utf-8")
    assert _send_heartbeat(env).status_code == 200
    assert env["core_http"].get("/api/health").json()["status"] == "HEALTHY"

    token = _begin_restore(env, sid)

    # Invariant: during MAINTENANCE (before data restore finishes), BOTH user writes
    # and worker heartbeats are fenced with 423.
    assert env["core_http"].get("/api/system/state").json()["state"] == "MAINTENANCE"
    assert env["core_http"].post("/api/jobs", json={"topic": "blocked-in-maintenance"}).status_code == 423
    assert _send_heartbeat(env).status_code == 423

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            env["backup_client"].post,
            f"/snapshots/{sid}/restore/confirm",
            params={"token": token},
        )

        # Wait until data restore has completed and CORE enters RECOVERING while
        # /api/health reports UNHEALTHY due to the restored expired heartbeat.
        def in_recovering_with_stale_worker() -> bool:
            state = env["core_http"].get("/api/system/state").json()
            if state.get("state") != "RECOVERING":
                return False
            health = env["core_http"].get("/api/health").json()
            return (
                health.get("status") == "UNHEALTHY"
                and health.get("checks", {}).get("local_worker") == [False, "health_worker_not_ready"]
            )

        _wait_for(in_recovering_with_stale_worker, timeout=10.0)

        # During RECOVERING, regular writes remain fenced with 423.
        assert env["core_http"].post("/api/jobs", json={"topic": "blocked-in-recovering"}).status_code == 423
        assert env["core_http"].put("/api/workers", json={"node_name": env["node_name"], "status": "READY"}).status_code == 423
        assert not future.done(), "Restore must not succeed while Worker heartbeat is expired"

        # Worker delivers a fresh heartbeat while CORE is in RECOVERING.
        hb = _send_heartbeat(env)
        assert hb.status_code == 200, hb.text
        assert hb.json()["accepted"] is True

        confirm = future.result(timeout=10.0)

    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["status"] == "done"
    assert data_file.read_text(encoding="utf-8") == "older-snapshot-data"
    assert env["core_http"].get("/api/health").json()["status"] == "HEALTHY"
    state = env["core_http"].get("/api/system/state").json()
    assert state["state"] == "NORMAL"
    assert state["operation_id"] is None
    progress = env["backup_client"].get(f"/snapshots/{sid}/restore/progress").json()
    assert progress["status"] == "done" and progress["progress"] == 100


def test_ac2_expired_heartbeat_never_counts_and_missing_worker_times_out_to_emergency(
    restore_heartbeat_env, monkeypatch,
):
    """AC2 (timeout): an expired heartbeat is never treated as ready; when no fresh
    Worker heartbeat arrives within the bounded timeout, restore fails with explicit
    diagnostics and transitions CORE into EMERGENCY."""
    env = restore_heartbeat_env
    storage = env["storage"]
    monkeypatch.setenv("BACKUP_POST_RESTORE_HEALTH_TIMEOUT", "0.4")
    monkeypatch.setenv("BACKUP_POST_RESTORE_HEALTH_POLL_INTERVAL", "0.05")

    (storage / "record.txt").write_text("stale-worker-snapshot", encoding="utf-8")
    _write_worker_record(storage, env["node_name"], datetime.now(timezone.utc) - timedelta(minutes=15))
    sid = env["backup_client"].post("/snapshots", json={"job_id": "missing-worker"}).json()["snapshot_id"]

    # Bring CORE healthy before starting restore.
    assert _send_heartbeat(env).status_code == 200
    assert env["core_http"].get("/api/health").json()["status"] == "HEALTHY"

    token = _begin_restore(env, sid)
    started = time.monotonic()
    confirm = env["backup_client"].post(f"/snapshots/{sid}/restore/confirm", params={"token": token})
    elapsed = time.monotonic() - started

    assert confirm.status_code == 500, confirm.text
    assert 0.35 <= elapsed < 5.0, f"Expected bounded timeout around 0.4s, got {elapsed:.2f}s"
    progress = env["backup_client"].get(f"/snapshots/{sid}/restore/progress").json()
    assert progress["status"] == "error"
    assert "health check" in progress["message"]
    assert "timed out" in progress["message"]
    assert "local_worker=health_worker_not_ready" in progress["message"]
    assert env["core_http"].get("/api/system/state").json()["state"] == "EMERGENCY"
    operations = json.loads((env["restore_state"] / "operations.json").read_text(encoding="utf-8"))
    assert operations[sid]["emergency_confirmed"] is True
    assert env["core_http"].get("/api/health").json()["status"] == "UNHEALTHY"


def test_ac2_infrastructure_health_failure_fails_fast_with_diagnostics_and_emergency(
    restore_heartbeat_env, monkeypatch,
):
    """AC2 (real failure): when a required non-worker service (e.g. Ollama) fails,
    restore must NOT wait out the worker timeout; it fails immediately with diagnostics
    naming the failed check and transitions CORE into EMERGENCY."""
    env = restore_heartbeat_env
    storage = env["storage"]
    monkeypatch.setenv("BACKUP_POST_RESTORE_HEALTH_TIMEOUT", "10")
    monkeypatch.setenv("BACKUP_POST_RESTORE_HEALTH_POLL_INTERVAL", "0.05")

    (storage / "record.txt").write_text("infra-failure-snapshot", encoding="utf-8")
    _write_worker_record(storage, env["node_name"], datetime.now(timezone.utc) - timedelta(minutes=10))
    sid = env["backup_client"].post("/snapshots", json={"job_id": "infra-fail"}).json()["snapshot_id"]

    # Simulate a genuine Ollama outage during post-restore verification.
    monkeypatch.setattr(health_checks, "check_ollama", lambda *a, **k: (False, "health_endpoint_unavailable"))

    token = _begin_restore(env, sid)
    started = time.monotonic()
    confirm = env["backup_client"].post(f"/snapshots/{sid}/restore/confirm", params={"token": token})
    elapsed = time.monotonic() - started

    assert confirm.status_code == 500, confirm.text
    assert elapsed < 3.0, f"Real infrastructure failure should fail fast, took {elapsed:.2f}s"
    progress = env["backup_client"].get(f"/snapshots/{sid}/restore/progress").json()
    assert progress["status"] == "error"
    assert "health check" in progress["message"]
    assert "ollama=health_endpoint_unavailable" in progress["message"]
    assert "timed out" not in progress["message"]
    assert env["core_http"].get("/api/system/state").json()["state"] == "EMERGENCY"


def test_ac2_gpu_worker_readiness_polling_and_non_worker_fast_fail(monkeypatch, tmp_path):
    """Unit check for gpu_worker readiness polling, fast-fail on comfyui failure,
    and mid-wait CORE unreachability diagnostics."""
    dest = tmp_path / "storage"
    dest.mkdir()
    monkeypatch.setenv("BACKUP_CORE_URL", "http://core.local:8000")
    monkeypatch.setenv("BACKUP_POST_RESTORE_HEALTH_TIMEOUT", "2")
    monkeypatch.setenv("BACKUP_POST_RESTORE_HEALTH_POLL_INTERVAL", "0.01")

    # 1) Only gpu_worker fails first, then becomes healthy on 3rd poll.
    responses = iter([
        {
            "status": "UNHEALTHY",
            "checks": {
                "required": ["comfyui", "core_api", "cuda", "docker", "gpu", "gpu_worker", "postgres", "redis"],
                "comfyui": [True, "HTTP 200"],
                "core_api": [True, "local"],
                "cuda": [True, "health_gpu_compute"],
                "docker": [True, "ok"],
                "gpu": [True, "RTX"],
                "gpu_worker": [False, "health_worker_not_ready"],
                "postgres": [True, "SELECT 1 = 1"],
                "redis": [True, "PING = PONG"],
            },
        },
        {
            "status": "UNHEALTHY",
            "checks": {
                "required": ["comfyui", "core_api", "cuda", "docker", "gpu", "gpu_worker", "postgres", "redis"],
                "comfyui": [True, "HTTP 200"],
                "core_api": [True, "local"],
                "cuda": [True, "health_gpu_compute"],
                "docker": [True, "ok"],
                "gpu": [True, "RTX"],
                "gpu_worker": [False, "health_worker_not_ready"],
                "postgres": [True, "SELECT 1 = 1"],
                "redis": [True, "PING = PONG"],
            },
        },
        {"status": "HEALTHY", "checks": {"required": ["gpu_worker"], "gpu_worker": [True, "health_worker_ready"]}},
    ])
    monkeypatch.setattr(backup_service, "_core_health_check", lambda: next(responses))
    backup_service._post_restore_health_check({"storage": dest})

    # 2) Non-worker failure (comfyui) alongside gpu_worker fails on the very first poll.
    calls = {"count": 0}

    def comfyui_down():
        calls["count"] += 1
        return {
            "status": "UNHEALTHY",
            "checks": {
                "required": ["comfyui", "gpu_worker"],
                "comfyui": [False, "health_endpoint_unavailable"],
                "gpu_worker": [False, "health_worker_not_ready"],
            },
        }

    monkeypatch.setattr(backup_service, "_core_health_check", comfyui_down)
    with pytest.raises(RuntimeError, match="comfyui=health_endpoint_unavailable"):
        backup_service._post_restore_health_check({"storage": dest})
    assert calls["count"] == 1

    # 3) CORE becoming unreachable mid-wait raises explicit diagnostics.
    flaky = iter([
        {
            "status": "UNHEALTHY",
            "checks": {"required": ["local_worker"], "local_worker": [False, "health_worker_not_ready"]},
        },
        None,
    ])
    monkeypatch.setattr(backup_service, "_core_health_check", lambda: next(flaky))
    with pytest.raises(RuntimeError, match="CORE became unreachable"):
        backup_service._post_restore_health_check({"storage": dest})


def test_ac3_regression_fresh_snapshot_restores_data_and_enters_normal(restore_heartbeat_env):
    """AC3: restoring a fresh snapshot (heartbeat still valid) restores data and
    completes into NORMAL without waiting for another heartbeat."""
    env = restore_heartbeat_env
    storage = env["storage"]
    data_file = storage / "record.txt"
    data_file.write_text("fresh-approved-payload", encoding="utf-8")

    # Fresh heartbeat recorded inside the snapshot.
    assert _send_heartbeat(env).status_code == 200
    assert env["core_http"].get("/api/health").json()["status"] == "HEALTHY"
    sid = env["backup_client"].post("/snapshots", json={"job_id": "fresh-snap"}).json()["snapshot_id"]

    # Mutate data after taking snapshot.
    data_file.write_text("mutated-after-fresh-snapshot", encoding="utf-8")
    extra_file = storage / "extra-after-snapshot.txt"
    extra_file.write_text("should-be-removed", encoding="utf-8")

    token = _begin_restore(env, sid)
    confirm = env["backup_client"].post(f"/snapshots/{sid}/restore/confirm", params={"token": token})

    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["status"] == "done"
    assert data_file.read_text(encoding="utf-8") == "fresh-approved-payload"
    assert not extra_file.exists()
    assert env["core_http"].get("/api/system/state").json()["state"] == "NORMAL"
    assert env["core_http"].get("/api/health").json()["status"] == "HEALTHY"
