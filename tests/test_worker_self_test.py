from adapters.comfyui import ComfyUIAdapter
from core.dispatcher import available_worker
from core.models import Job, JobStatus
from worker.service import role_self_test


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
    base = {"node_name": "gpu-01", "status": "FREE", "last_seen": "2026-08-25T00:00:00+00:00",
            "vram_mb": 16000, "capabilities": ["image_generation"], "supported_workflows": ["*"]}
    # Patch the timestamp into the future so heartbeat expiry is not the subject of this test.
    base["last_seen"] = "2999-08-25T00:00:00+00:00"
    assert available_worker([base], job()) is None
    base["self_test"] = {"status": "PASSED"}
    assert available_worker([base], job())["node_name"] == "gpu-01"
