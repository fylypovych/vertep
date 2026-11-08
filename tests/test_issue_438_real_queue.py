"""#432 AC3: simultaneous HTTP claims against the real queue and the real claim code."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import turns
from core.api import tasks
from core.models import JobStatus
from core.pipeline import JobStore
from core.queue import TaskQueue

WORKERS = "abcdef"


def _worker(name):
    return {"node_name": name, "role": "text", "capabilities": ["text_generation"],
            "supported_tasks": ["text"], "vram_mb": 0, "status": "READY",
            "last_seen": datetime.now(timezone.utc).isoformat()}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    store, queue = JobStore(root=str(tmp_path)), TaskQueue()
    queue._redis = None
    for name in WORKERS:
        store.save_worker(_worker(name))
    monkeypatch.setattr(tasks, "store", store)
    monkeypatch.setattr(tasks, "task_queue", queue)
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, request: True)
    turns._yielding_since.clear()
    app = FastAPI()
    app.include_router(tasks.router)
    return TestClient(app), store, queue


def test_simultaneous_claims_neither_duplicate_nor_lose_a_real_task(setup):
    client, store, queue = setup
    for index in range(3):
        job = store.create("тема", "character", 5)
        task = queue.enqueue({"task_id": f"script-{index}", "job_id": job.job_id, "task": "script",
                              "kind": "script"})
        job.script_task_id = task["task_id"]
        store.update(job, JobStatus.SCRIPT_QUEUED, "queued")
    barrier = Barrier(len(WORKERS))

    def post(name):
        barrier.wait()
        return client.post("/api/tasks/claim", json={"node_name": name, "vram_mb": 0}).json()

    with ThreadPoolExecutor(len(WORKERS)) as pool:
        answers = list(pool.map(post, WORKERS))
    claimed = [answer["task"]["task_id"] for answer in answers if answer.get("task")]
    assert claimed, answers
    assert len(claimed) == len(set(claimed)), answers
    assert len(claimed) + queue.depth() == 3, answers
    holders = [name for name in WORKERS if store.workers[name].get("current_task")]
    assert sorted(store.workers[name]["current_task"] for name in holders) == sorted(claimed)
