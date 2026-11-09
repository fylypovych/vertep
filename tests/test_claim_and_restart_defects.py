"""Two defects found while testing #438: a double claim by one node and a lost script task."""
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from core.api import tasks
from core.models import JobStatus
from core.pipeline import JobStore
from tests.test_issue_438_claims import client  # noqa: F401 - fixture


def test_one_node_claiming_twice_at_once_gets_one_task(client, monkeypatch):  # noqa: F811
    queue, barrier = ["t1", "t2"], Barrier(2)

    def claim_one(payload):
        worker = tasks.store.workers[payload.node_name]
        if worker.get("current_task"):
            return {"task": None, "worker_state": "BUSY"}
        time.sleep(0.2)  # widen the window between the check and the assignment
        worker["current_task"] = queue.pop(0)
        return {"task": worker["current_task"]}
    monkeypatch.setattr(tasks, "_claim_task", claim_one)

    def post(_):
        barrier.wait()
        return client.post("/api/tasks/claim", json={"node_name": "a", "vram_mb": 0}).json()

    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(post, range(2)))
    assert len([answer for answer in answers if answer.get("task")]) == 1


def _redispatched(monkeypatch, tmp_path, task_id):
    from core import lifecycle
    store = JobStore(root=str(tmp_path))
    job = store.create("topic", "char", 5)
    job.status, job.script_task_id = JobStatus.SCRIPT_QUEUED, task_id
    submitted = []
    monkeypatch.setattr(lifecycle, "store", store)
    monkeypatch.setattr(lifecycle, "dispatch_allowed", lambda: True)
    monkeypatch.setattr(lifecycle, "executor", SimpleNamespace(submit=lambda fn, j: submitted.append(j.job_id)))
    lifecycle._redispatch_after_restart()
    return job.job_id in submitted


def test_script_queued_job_without_its_task_id_is_redispatched_after_restart(monkeypatch, tmp_path):
    # CORE stopped between queueing the script task and saving its id.
    assert _redispatched(monkeypatch, tmp_path, None)


def test_script_queued_job_with_its_task_id_keeps_waiting_after_restart(monkeypatch, tmp_path):
    assert not _redispatched(monkeypatch, tmp_path, "task-1")
