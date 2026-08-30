import json
from datetime import datetime, timedelta, timezone

from adapters.comfyui import ComfyUIAdapter
from core.dispatcher import available_worker, current_tested_capabilities
from core.models import Job, JobStatus, worker_transition_allowed
from worker.service import request_local_update, role_self_test


def job() -> Job:
    return Job(job_id="job-1", topic="test", character_id="character", priority=5,
               status=JobStatus.NEW, created_at="2026-08-25T00:00:00+00:00")


def test_demo_gpu_and_backup_self_tests(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_MODE", "true")
    assert role_self_test("gpu", {}, ComfyUIAdapter())["status"] == "PASSED"
    monkeypatch.setenv("BACKUP_ROOT", str(tmp_path))
    assert role_self_test("backup", {})["status"] == "PASSED"
    assert not list(tmp_path.iterdir())


def test_dispatch_requires_successful_self_test(monkeypatch):
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "true")
    now = datetime.now(timezone.utc)
    base = {"node_name": "gpu-01", "status": "FREE", "last_seen": now.isoformat(), "role": "gpu",
            "vram_mb": 16000, "capabilities": ["image_generation"], "supported_workflows": ["*"]}
    assert available_worker([base], job()) is None
    base["self_test"] = {"status": "PASSED", "role": "gpu", "checked_at": now.isoformat()}
    base["tested_capabilities"] = ["image_generation"]
    assert available_worker([base], job())["node_name"] == "gpu-01"


def test_attestation_expires_and_cannot_add_capabilities(monkeypatch):
    monkeypatch.setenv("WORKER_SELF_TEST_MAX_AGE_SECONDS", "60")
    now = datetime.now(timezone.utc)
    worker = {"role": "gpu", "capabilities": ["image_generation"],
              "tested_capabilities": ["image_generation", "publishing"],
              "self_test": {"status": "PASSED", "role": "gpu", "checked_at": now.isoformat()}}
    assert current_tested_capabilities(worker, now) == {"image_generation"}
    worker["self_test"]["checked_at"] = (now - timedelta(seconds=61)).isoformat()
    assert not current_tested_capabilities(worker, now)


def test_worker_state_machine_rejects_privilege_transitions():
    assert worker_transition_allowed("READY", "BUSY")
    assert worker_transition_allowed("ONLINE", "READY")
    assert not worker_transition_allowed("QUARANTINED", "READY")
    assert not worker_transition_allowed("REVOKED", "READY")


def test_scheduler_scores_healthy_candidates(monkeypatch):
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "true")
    now = datetime.now(timezone.utc)
    attestation = {"status": "PASSED", "role": "gpu", "checked_at": now.isoformat()}
    common = {"status": "READY", "last_seen": now.isoformat(), "role": "gpu",
              "capabilities": ["image_generation"], "tested_capabilities": ["image_generation"],
              "supported_workflows": ["*"], "self_test": attestation}
    busy_gpu = {**common, "node_name": "gpu-busy", "vram_mb": 24000,
                "free_vram_mb": 8000, "gpu_load": 90}
    idle_gpu = {**common, "node_name": "gpu-idle", "vram_mb": 16000,
                "free_vram_mb": 12000, "gpu_load": 10}
    assert available_worker([busy_gpu, idle_gpu], job())["node_name"] == "gpu-idle"


def test_coordinated_update_request_is_idempotent(monkeypatch, tmp_path):
    request_root = tmp_path / "update" / "requests"
    monkeypatch.setenv("UPDATE_REQUEST_DIR", str(request_root))
    request_local_update("1.2.3")
    request_local_update("1.2.3")
    assert len(list(request_root.glob("*.json"))) == 1
    assert (request_root.parent / "worker-update-target").read_text() == "1.2.3"


def test_rollback_request_is_idempotent(monkeypatch, tmp_path):
    request_root = tmp_path / "update" / "requests"
    monkeypatch.setenv("UPDATE_REQUEST_DIR", str(request_root))
    request_local_update("1.1.0", action="rollback")
    request_local_update("1.1.0", action="rollback")
    request = json.loads(next(request_root.glob("*.json")).read_text())
    assert request["action"] == "rollback"
    assert len(list(request_root.glob("*.json"))) == 1
