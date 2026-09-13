"""Queue semantics, scheduling and dispatcher production behavior (issue i.0.0.0.26).

Covers: scheduled/ready/inflight/dead-letter contracts, pagination, refresh
(generation), retry-conflict semantics, lease expiry / worker loss / dead-letter
determinism, capability/self-test gating, load score, locality and fair
scheduling, maintenance/update gating, and Dashboard/Jobs/Queue consistency.

Tests run fully in-process (local queue backend + TempFile JobStore).
"""
from datetime import datetime, timedelta, timezone
import time

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.dispatcher import available_worker, compute_load_score
from core.models import Job, JobStatus
from core.state import store, task_queue
from core.system_state import (SystemState, dispatch_allowed, new_job_status,
                                set_system_state)


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _enqueue(**overrides) -> dict:
    task = {"job_id": "job-1", "task": "image", "priority": 5, "scene_id": "scene-1"}
    task.update(overrides)
    return task_queue.enqueue(task)


def _worker(**overrides) -> dict:
    now = _now_iso()
    worker = {
        "node_name": "gpu-01", "status": "FREE", "role": "gpu",
        "last_seen": now, "vram_mb": 16000, "free_vram_mb": 12000,
        "gpu_load": 10.0, "cpu_load": 0.1,
        "capabilities": ["image_generation"],
        "tested_capabilities": ["image_generation"],
        "supported_workflows": ["*"],
        "self_test": {"status": "PASSED", "role": "gpu", "checked_at": now},
    }
    worker.update(overrides)
    return worker


def _job(**overrides) -> Job:
    job = Job(job_id="job-1", topic="test", character_id="character",
              priority=5, status=JobStatus.NEW, created_at=_now_iso())
    for k, v in overrides.items():
        setattr(job, k, v)
    return job


def _client():
    return TestClient(app, raise_server_exceptions=False)


# ── contract ────────────────────────────────────────────────────


def test_queue_contract(client):
    scheduled_job = store.create("scheduled", "did_samogon", 5)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    scheduled_job.scheduled_for = future
    _enqueue(job_id=scheduled_job.job_id, priority=1)          # stays ready
    _enqueue(job_id=scheduled_job.job_id, priority=9)          # becomes inflight
    dl_enq = _enqueue(job_id=scheduled_job.job_id, priority=8)
    task_queue.claim()             # pri-9 → inflight (kept)
    dead = task_queue.claim()      # pri-8 → inflight
    task_queue.ack(dead["task_id"])
    task_queue.dead_letter(dead, "GPU error")  # pri-8 → dead letter
    state = client.get("/api/tasks/queue").json()
    assert state["generation"] > 0
    assert state["total"]["ready"] == 1
    assert state["total"]["inflight"] == 1
    assert state["total"]["dead_letter"] == 1
    assert state["total"]["scheduled"] == 1
    assert state["pagination"]["dead_letter_total"] == 1
    assert state["ready"][0]["job_status"] == JobStatus.NEW.value
    assert state["scheduled"][0]["job_id"] == scheduled_job.job_id
    assert state["dead_letter"][0]["job_status"] == JobStatus.NEW.value


def test_queue_dead_letter_pagination(client):
    for index in range(5):
        enq = _enqueue(job_id="job-p", priority=index + 1)
        task_queue.dead_letter(enq, f"e{index}")
    body = client.get("/api/tasks/queue", params={"limit": 2, "offset": 1}).json()
    assert body["pagination"]["dead_letter_total"] == 5
    assert len(body["dead_letter"]) == 2
    assert body["total"]["dead_letter"] == 5


def test_queue_refresh_generation(client):
    before = client.get("/api/tasks/queue").json()["generation"]
    _enqueue()
    after = client.get("/api/tasks/queue").json()
    assert after["generation"] > before
    assert client.get("/api/tasks/queue",
                      params={"refresh": after["generation"]}).json()["unchanged"] is True
    assert client.get("/api/tasks/queue",
                      params={"refresh": 0}).json()["unchanged"] is False# ── lease/determinism ────────────────────────────────────────


def test_lease_expiry_requeues_deterministically(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")
    q = task_queue
    high = q.enqueue({"job_id": "h", "priority": 9, "scene_id": "s1"})
    low = q.enqueue({"job_id": "l", "priority": 1, "scene_id": "s2"})
    q.claim(lease_seconds=1)
    q.claim(lease_seconds=1)
    expired = q.requeue_expired(now=time.time() + 2)
    assert {item["task_id"] for item in expired} == {high["task_id"], low["task_id"]}
    assert expired[0]["priority"] > expired[1]["priority"]
    assert q.inflight_depth() == 0
    assert q.depth() == 2


def test_lease_stable_task_id_on_release(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")
    q = task_queue
    enq = q.enqueue({"job_id": "j", "priority": 5, "scene_id": "s1"})
    claimed = q.claim(lease_seconds=1)
    q.release(claimed["task_id"])
    assert q.has_task(claimed["task_id"])
    assert enq["task_id"] == claimed["task_id"]


def test_dead_letter_requeue_deterministic(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")
    q = task_queue
    enq = q.enqueue({"job_id": "j", "scene_id": "s1", "priority": 5})
    claimed = q.claim(lease_seconds=30)
    assert claimed["task_id"] == enq["task_id"]
    q.ack(claimed["task_id"])
    q.dead_letter(claimed, "boom")
    assert q.dead_letters()[0]["error"] == "boom"
    retried = q.requeue_dead_letter(claimed["task_id"])
    assert retried is not None and retried["task_id"] != claimed["task_id"]
    assert q.dead_letters() == []
    assert q.has_task(retried["task_id"]) and q.depth() == 1


# ── scheduling ──────────────────────────────────────────────────


def test_load_score_ranks_busy_higher():
    idle = compute_load_score({"status": "FREE", "gpu_load": 5, "cpu_load": 0.05})
    busy = compute_load_score({"status": "BUSY", "current_task": "t",
                                "gpu_load": 90, "cpu_load": 0.9})
    assert idle < busy


def test_dispatch_locality(monkeypatch):
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "false")
    tagged = _worker(node_name="gpu-tagged", scheduler_tags=["fast"])
    untagged = _worker(node_name="gpu-untagged", scheduler_tags=[])
    chosen = available_worker([tagged, untagged], _job(required_tags=["fast"]))
    assert chosen and chosen["node_name"] == "gpu-tagged"
    assert available_worker([untagged], _job(required_tags=["fast"])) is None


def test_dispatch_fair_round_robin(monkeypatch):
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "false")
    a = _worker(node_name="gpu-a", scheduler_dispatch_count=5, free_vram_mb=12000)
    b = _worker(node_name="gpu-b", scheduler_dispatch_count=0, free_vram_mb=12000)
    assert available_worker([a, b], _job())["node_name"] == "gpu-b"


def test_dispatch_self_test_gating(monkeypatch):
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "true")
    now = _now_iso()
    worker = _worker(status="FREE")
    worker.pop("self_test")
    worker.pop("tested_capabilities")
    assert available_worker([worker], _job()) is None
    worker["self_test"] = {"status": "PASSED", "role": "gpu", "checked_at": now}
    worker["tested_capabilities"] = ["image_generation"]
    assert available_worker([worker], _job())["node_name"] == "gpu-01"


# ── retry conflict ──────────────────────────────────────────────


def test_retry_conflict_inflight_returns_409(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")
    enq = _enqueue()
    task_queue.claim()
    assert client.post(f"/api/tasks/dead-letter/{enq['task_id']}/retry").status_code == 409


def test_retry_missing_returns_404(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")
    assert client.post("/api/tasks/dead-letter/nonexistent/retry").status_code == 404


# ── maintenance gating ──────────────────────────────────────────


def test_maintenance_blocks_claim_and_keeps_job(client, monkeypatch):
    monkeypatch.setenv("NODE_API_TOKEN", "tok")
    job = store.create("kept", "did_samogon", 5)
    set_system_state(SystemState.MAINTENANCE, "test window")
    try:
        assert not dispatch_allowed()
        assert new_job_status() == "WAITING_FOR_SYSTEM"
        resp = client.post("/api/tasks/claim",
                           json={"node_name": "gpu-1", "capabilities": ["image_generation"]},
                           headers={"x-vertep-token": "tok"})
        assert resp.status_code == 200
        assert resp.json()["task"] is None
        assert resp.json()["system_state"] == "MAINTENANCE"
        assert store.jobs.get(job.job_id) is not None
    finally:
        set_system_state(SystemState.NORMAL, "restore")


def test_update_gating_blocks_dispatch(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")
    set_system_state(SystemState.UPDATING, "rollout")
    try:
        assert not dispatch_allowed()
        assert new_job_status() == "WAITING_FOR_SYSTEM"
    finally:
        set_system_state(SystemState.NORMAL, "restore")
    assert dispatch_allowed()
    assert new_job_status() == "NEW"


# ── consistency ─────────────────────────────────────────────────


def test_queue_and_metrics_counts_consistent(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")
    t1 = _enqueue(job_id="job-c", priority=9)
    _enqueue(job_id="job-c", priority=1)
    task_queue.claim()
    task_queue.ack(t1["task_id"])
    task_queue.dead_letter(t1, "boom")
    state = client.get("/api/tasks/queue").json()
    metrics = client.get("/api/metrics").json()
    assert state["total"]["ready"] == 1
    assert state["total"]["inflight"] == 0
    assert state["total"]["dead_letter"] == 1
    assert metrics.get("queue_ready") == 1
    assert metrics.get("queue_inflight") == 0
    assert metrics.get("queue_dead_letter") == 1