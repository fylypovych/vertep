"""Queue semantics, scheduling and dispatcher production behavior (issue i.0.0.0.26).

Covers: scheduled/ready/inflight/dead-letter contracts, pagination, refresh
(generation), retry-conflict semantics, lease expiry / worker loss / dead-letter
determinism, capability/self-test gating, load score, locality and fair
scheduling, maintenance/update gating, and Dashboard/Jobs/Queue consistency.

Issue i.0.0.1.2 (#102): script/storyboard results are accepted only from the
node holding the whole claim lease, and two Jobs must reach two Workers through
the real queue without CORE consuming its own tasks.

Tests run fully in-process (local queue backend + TempFile JobStore).
"""
from datetime import datetime, timedelta, timezone
import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.dispatcher import available_worker, compute_load_score
from core.models import Job, JobStatus
from core.state import store, task_queue
from core.storyboard import StoryboardService
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


def test_lease_expiry_requeues_deterministically():
    assert task_queue.backend == "local"
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


def test_lease_stable_task_id_on_release():
    assert task_queue.backend == "local"
    q = task_queue
    enq = q.enqueue({"job_id": "j", "priority": 5, "scene_id": "s1"})
    claimed = q.claim(lease_seconds=1)
    q.release(claimed["task_id"])
    assert q.has_task(claimed["task_id"])
    assert enq["task_id"] == claimed["task_id"]


def test_dead_letter_requeue_deterministic():
    assert task_queue.backend == "local"
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


def test_retry_conflict_inflight_returns_409(client):
    assert task_queue.backend == "local"
    enq = _enqueue()
    task_queue.claim()
    assert client.post(f"/api/tasks/dead-letter/{enq['task_id']}/retry").status_code == 409


def test_retry_missing_returns_404(client):
    assert task_queue.backend == "local"
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


# ── storyboard task dispatch (Issue #82) ────────────────────────


def test_storyboard_queue_keeps_task_claimable_by_text_worker(client, monkeypatch):
    """CORE must not consume the storyboard task it just queued.

    ``StoryboardService.queue()`` used to self-claim and ack its own task, so no
    Text Worker could ever see it and the Job stayed in STORYBOARD_QUEUED forever.
    """
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    job = store.create("storyboard dispatch", "did_samogon", 5)
    StoryboardService(store).queue(job)

    assert job.status == JobStatus.STORYBOARD_QUEUED
    assert job.storyboard_task_id
    assert task_queue.depth() >= 1
    assert not task_queue._inflight


def test_storyboard_claim_does_not_require_job_vram(client, monkeypatch):
    """A GPU-less Text Worker must be eligible for the storyboard task.

    Every other non-GPU claim branch passes ``min_vram_mb=0``; the storyboard
    branch inherited the Job's GPU requirement and never matched a text node.
    """
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client.post("/api/workers/heartbeat", json={
        "node_name": "text-node", "vram_mb": 0, "role": "text",
        "capabilities": ["text_generation"], "supported_tasks": ["text"],
    })
    # A GPU-heavy job: the storyboard task is text work and must ignore this.
    job = store.create("storyboard vram", "did_samogon", 5, "web", "image", 24576)
    StoryboardService(store).queue(job)

    task = client.post("/api/tasks/claim",
                       json={"node_name": "text-node", "vram_mb": 0}).json()["task"]
    assert task is not None
    assert task["task"] == "storyboard"
    assert task["task_id"] == job.storyboard_task_id
    assert store.jobs[job.job_id].status == JobStatus.STORYBOARD_GENERATING
    task_queue.ack(task["task_id"])


def test_update_gating_blocks_dispatch():
    assert task_queue.backend == "local"
    set_system_state(SystemState.UPDATING, "rollout")
    try:
        assert not dispatch_allowed()
        assert new_job_status() == "WAITING_FOR_SYSTEM"
    finally:
        set_system_state(SystemState.NORMAL, "restore")
    assert dispatch_allowed()
    assert new_job_status() == "NEW"


# ── consistency ─────────────────────────────────────────────────


def test_queue_and_metrics_counts_consistent(client):
    assert task_queue.backend == "local"
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


# ── result ownership (issue i.0.0.1.2 / #102) ──────────────────


def _text_worker(client, name: str) -> None:
    client.post("/api/workers/heartbeat", json={
        "node_name": name, "vram_mb": 0, "role": "text",
        "capabilities": ["text_generation"], "supported_tasks": ["text"],
    })


def _storyboard_payload(version: int) -> dict:
    return {
        "filename": "storyboard.json",
        "kind": "storyboard",
        "data_base64": base64.b64encode(json.dumps({
            "version": version, "title": "Історія", "description": "Опис",
            "hashtags": ["#vertep"],
            "scenes": [{"index": 1, "prompt": "Кадр", "video_prompt": "Кадр",
                        "voiceover": "Текст", "duration": 5}],
            "prompt_version": "1.0", "model": "test", "status": "pending_approval",
            "image_version": 1, "image_status": "pending",
        }, ensure_ascii=False).encode("utf-8")).decode("ascii"),
    }


def _claim_text_task(client, node_name: str, job_id: str, kind: str) -> dict:
    for _ in range(100):
        claimed = client.post("/api/tasks/claim",
                              json={"node_name": node_name, "vram_mb": 0}).json().get("task")
        if claimed and claimed.get("task") == kind and claimed.get("job_id") == job_id:
            return claimed
        time.sleep(0.01)
    raise AssertionError(f"{node_name} did not claim the {kind} task of {job_id}")


def _queue_script_task(job) -> str:
    """Put a Job into the script claim path without relying on the async dispatch."""
    from core.api.job_helpers import _enqueue_script_task

    _enqueue_script_task(store, job)
    if job.status != JobStatus.SCRIPT_QUEUED:
        store.update(job, JobStatus.SCRIPT_QUEUED, "SCRIPT TASK QUEUED")
    assert job.script_task_id
    return job.script_task_id


def test_two_storyboard_jobs_flow_from_real_queue_to_worker_claims(client, monkeypatch):
    """Issue i.0.0.1.2 (1): two Jobs must reach two Text Workers through the real
    queue, with ``StoryboardService.queue`` untouched.

    The Issue #82 exclusive-claim test enqueues the task by hand, which cannot
    prove that the *real* ``StoryboardService.queue()`` leaves its task claimable
    by a Worker: CORE must not claim or ack the task it just queued, otherwise
    the Job stays in STORYBOARD_QUEUED forever on a real deployment.
    """
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("VERTEP_DEMO", "false")
    for name in ("text-a", "text-b"):
        _text_worker(client, name)

    jobs = [store.create("job one", "did_samogon", 5),
            store.create("job two", "did_samogon", 5)]
    for job in jobs:
        StoryboardService(store).queue(job)

    # Both tasks sit on the ready queue: neither was consumed nor leased by CORE.
    for job in jobs:
        assert job.status == JobStatus.STORYBOARD_QUEUED
        assert task_queue.has_task(job.storyboard_task_id)
        assert not task_queue.inflight_has(job.storyboard_task_id)
        assert store.workers["text-a"].get("current_task") is None
        assert store.workers["text-b"].get("current_task") is None
    assert task_queue.depth() == 2

    first = _claim_text_task(client, "text-a", jobs[0].job_id, "storyboard")
    second = _claim_text_task(client, "text-b", jobs[1].job_id, "storyboard")
    assert {first["task_id"], second["task_id"]} == {
        jobs[0].storyboard_task_id, jobs[1].storyboard_task_id}

    for name, claimed in (("text-a", first), ("text-b", second)):
        job = store.jobs[claimed["job_id"]]
        # QUEUED -> GENERATING on claim, so the Job lifecycle and the queue agree.
        assert job.status == JobStatus.STORYBOARD_GENERATING
        assert store.workers[name]["current_task"] == claimed["task_id"]
        assert store.workers[name]["current_job"] == job.job_id
        assert task_queue.inflight_has(claimed["task_id"])
    assert task_queue.depth() == 0


def test_script_result_is_accepted_only_from_the_claim_holder(client, monkeypatch):
    """Issue i.0.0.1.2 (2): a script result must be fenced on owner + lease.

    The script branch used to clear the Worker metadata and ``job.script_task_id``
    before proving anything about the submitter, so a foreign node could complete
    the task, release the real claim holder and get the task acked.
    """
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("VERTEP_DEMO", "false")
    for name in ("text-a", "text-b"):
        _text_worker(client, name)

    job = store.create("script ownership", "did_samogon", 5)
    task_id = _queue_script_task(job)
    claimed = _claim_text_task(client, "text-a", job.job_id, "script")
    assert claimed["task_id"] == task_id
    assert job.status == JobStatus.SCRIPT_GENERATING
    assert task_queue.inflight_has(task_id)

    from worker import role_executor
    [artifact] = role_executor.execute_role_task("text", claimed)

    def submit(node_name: str, *, cancelled: bool = False):
        return client.post("/api/tasks/result", json={
            "job_id": job.job_id, "task_id": task_id, "node_name": node_name,
            "success": True, "cancelled": cancelled, "artifacts": [artifact],
        })

    # A registered node that never claimed the task changes nothing.
    assert submit("text-b").status_code == 200
    assert job.status == JobStatus.SCRIPT_GENERATING
    assert job.script is None
    assert job.script_task_id == task_id
    assert task_queue.inflight_has(task_id)
    assert store.workers["text-b"].get("current_task") is None
    assert store.workers["text-a"]["current_task"] == task_id
    assert store.workers["text-a"]["status"] == "BUSY"

    # A cancelled result is a terminal late answer and may not complete the task.
    assert submit("text-a", cancelled=True).status_code == 200
    assert job.status == JobStatus.SCRIPT_GENERATING
    assert job.script is None
    assert task_queue.inflight_has(task_id)

    # The claim holder itself is accepted: cleanup, ack and release happen here.
    assert submit("text-a").status_code == 200
    assert job.status == JobStatus.SCRIPT_PENDING_APPROVAL
    assert job.script
    assert job.script_task_id is None
    assert not task_queue.inflight_has(task_id)
    assert store.workers["text-a"].get("current_task") is None
    assert store.workers["text-a"]["status"] != "BUSY"


def test_script_result_after_lease_expiry_is_rejected(client, monkeypatch):
    """Issue i.0.0.1.2 (2): an expired lease fences even the former owner.

    Re-queued by the watchdog, the task is no longer in flight: a late result
    must not complete it, ack it or touch the Job, and the ready entry survives
    for the next claim.
    """
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("VERTEP_DEMO", "false")
    _text_worker(client, "text-a")

    job = store.create("script lease", "did_samogon", 5)
    task_id = _queue_script_task(job)
    _claim_text_task(client, "text-a", job.job_id, "script")

    # The worker dies while holding the claim: the lease expires and the task
    # is handed back to the ready queue.
    inflight = task_queue._inflight[task_id]
    expired = task_queue.requeue_expired(now=(inflight[0] + 1) if not task_queue._redis else 0.0)
    assert any(item.get("task_id") == task_id for item in expired)
    assert not task_queue.inflight_has(task_id)
    assert task_queue.has_task(task_id)

    from core.api.job_helpers import _script_task_for
    from worker import role_executor
    claimed = dict(_script_task_for(job, "", None), task_id=task_id)
    [artifact] = role_executor.execute_role_task("text", claimed)
    response = client.post("/api/tasks/result", json={
        "job_id": job.job_id, "task_id": task_id, "node_name": "text-a",
        "success": True, "artifacts": [artifact],
    })
    assert response.status_code == 200
    assert job.status == JobStatus.SCRIPT_GENERATING
    assert job.script is None
    assert job.script_task_id == task_id
    # Neither acked nor dropped: the task is still claimable by the next worker.
    assert not task_queue.inflight_has(task_id)
    assert task_queue.has_task(task_id)


def test_storyboard_result_is_fenced_on_owner_version_and_payload(client, monkeypatch):
    """Issue i.0.0.1.2 (2): a storyboard result is accepted only from the lease
    holder, only for the queued version, only with a parseable payload, and never
    as a cancelled answer — and an accepted history cannot be rewritten.
    """
    from worker import role_executor
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("VERTEP_DEMO", "false")
    for name in ("text-a", "text-b"):
        _text_worker(client, name)

    job = store.create("storyboard ownership", "did_samogon", 5)
    StoryboardService(store).queue(job)
    task_id = job.storyboard_task_id
    claimed = _claim_text_task(client, "text-a", job.job_id, "storyboard")
    assert claimed["task_id"] == task_id
    assert job.status == JobStatus.STORYBOARD_GENERATING

    def submit(node_name, artifact, *, cancelled=False):
        return client.post("/api/tasks/result", json={
            "job_id": job.job_id, "task_id": task_id, "node_name": node_name,
            "success": True, "cancelled": cancelled, "artifacts": [artifact],
        })

    def assert_untouched():
        assert job.status == JobStatus.STORYBOARD_GENERATING
        assert job.storyboards == []
        assert job.storyboard_task_id == task_id
        assert task_queue.inflight_has(task_id)
        assert store.workers["text-a"]["current_task"] == task_id

    # foreign: a registered node that does not hold the claim.
    assert submit("text-b", _storyboard_payload(1)).status_code == 200
    assert_untouched()
    assert store.workers["text-b"].get("current_task") is None

    # stale: the payload may not pick the version it reports.
    assert submit("text-a", _storyboard_payload(99)).status_code == 200
    assert_untouched()

    # malformed: controlled 400, no half-written storyboard, no ack.
    broken = {"filename": "storyboard.json", "kind": "storyboard",
              "data_base64": base64.b64encode(b"not json at all").decode("ascii")}
    assert submit("text-a", broken).status_code == 400
    assert_untouched()
    incomplete = {"filename": "storyboard.json", "kind": "storyboard",
                  "data_base64": base64.b64encode(json.dumps({"version": 1}).encode()).decode("ascii")}
    assert submit("text-a", incomplete).status_code == 400
    assert_untouched()

    # cancelled result: a terminal late answer of an abort.
    assert submit("text-a", _storyboard_payload(1), cancelled=True).status_code == 200
    assert_untouched()

    # the lease holder with the queued version is accepted.
    assert submit("text-a", _storyboard_payload(1)).status_code == 200
    assert job.status == JobStatus.STORYBOARD_PENDING_APPROVAL
    assert [board.version for board in job.storyboards] == [1]
    assert not task_queue.inflight_has(task_id)
    assert store.workers["text-a"].get("current_task") is None

    # duplicate: a second answer to an already completed task never mutates the Job.
    assert submit("text-a", _storyboard_payload(2)).status_code == 409
    assert [board.version for board in job.storyboards] == [1]
    assert job.status == JobStatus.STORYBOARD_PENDING_APPROVAL

    # approved history stays immutable against a late result.
    service = StoryboardService(store)
    board = job.storyboards[0]
    # previews simulated as rendered, so the approval boundary can be crossed
    for scene in board.scenes:
        scene.image_artifact_id = f"art-{scene.index}"
    board.image_status = "ready"
    service.approve_images(job.job_id, board.version, "tester")
    service.approve(job.job_id, board.version, "tester")
    assert job.approved is True
    approved_script = job.script
    assert submit("text-a", _storyboard_payload(3)).status_code == 409
    assert [board.version for board in job.storyboards] == [1]
    assert job.storyboards[0].status == "approved"
    assert job.script == approved_script
    assert job.approved is True
    assert job.status == JobStatus.STORYBOARD_APPROVED
