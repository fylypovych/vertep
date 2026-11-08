"""An update must not wait for Jobs that cannot move while dispatch is paused."""
import pytest

from core import runtime
from core.api import system
from core.models import JobStatus
from core.pipeline import JobStore
from core.queue import TaskQueue


@pytest.fixture
def setup(monkeypatch, tmp_path):
    store, queue = JobStore(root=str(tmp_path)), TaskQueue()
    queue._redis = None
    monkeypatch.setattr(system, "store", store)
    monkeypatch.setattr(system, "task_queue", queue)
    monkeypatch.setattr(system, "registered_nodes", lambda: [])
    monkeypatch.setattr(system, "dispatch_allowed", lambda: False)
    monkeypatch.setattr(runtime, "_preparing", set())
    return store, queue


def _job(store, status):
    job = store.create("тема", "character", 5)
    store.update(job, status, "test")
    return job


def test_queued_and_approval_jobs_do_not_block_the_update(setup):
    store, queue = setup
    queued = _job(store, JobStatus.SCRIPT_QUEUED)
    queue.enqueue({"task_id": "t1", "job_id": queued.job_id, "task": "script"})
    review = _job(store, JobStatus.SCRIPT_PENDING_APPROVAL)
    answer = system.update_readiness()
    assert answer["ready"] is True, answer
    assert answer["active_jobs"] == []
    assert sorted(answer["waiting_jobs"]) == sorted([queued.job_id, review.job_id])
    # The paused task is kept for after the update, not dropped.
    assert queue.depth() == 1


def test_a_job_core_is_preparing_still_blocks(setup):
    store, _ = setup
    job = _job(store, JobStatus.SCRIPTING)
    runtime._preparing.add(job.job_id)
    answer = system.update_readiness()
    assert answer["ready"] is False
    assert answer["active_jobs"] == [job.job_id]


def test_in_flight_work_still_blocks(setup):
    store, queue = setup
    job = _job(store, JobStatus.SCRIPT_QUEUED)
    queue.enqueue({"task_id": "t1", "job_id": job.job_id, "task": "script"})
    assert queue.claim()
    assert system.update_readiness()["ready"] is False
