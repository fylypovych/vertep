"""#432 AC3/AC5: simultaneous HTTP claims and the order after CORE reloads its repository."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Lock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import turns
from core.api import tasks
from core.pipeline import JobStore


def _worker(name):
    return {"node_name": name, "role": "gpu", "capabilities": ["image_generation"], "vram_mb": 8192,
            "status": "READY", "last_seen": datetime.now(timezone.utc).isoformat()}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(tasks.router)
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, request: True)
    monkeypatch.setattr(tasks.store, "save_worker", lambda worker: None)
    monkeypatch.setattr(tasks.store, "workers", {name: _worker(name) for name in "abc"})
    turns._yielding_since.clear()
    return TestClient(app)


def test_simultaneous_claims_never_hand_out_one_task_twice(client, monkeypatch):
    queue, lock, barrier = ["t1", "t2"], Lock(), Barrier(6)

    def claim_one(payload):
        with lock:
            return {"task": queue.pop(0) if queue else None}
    monkeypatch.setattr(tasks, "_claim_task", claim_one)

    def post(name):
        barrier.wait()
        return client.post("/api/tasks/claim", json={"node_name": name, "vram_mb": 0}).json()

    with ThreadPoolExecutor(6) as pool:
        answers = list(pool.map(post, "abcabc"))
    claimed = [answer["task"] for answer in answers if answer.get("task")]
    assert sorted(claimed) == ["t1", "t2"]


def test_the_order_survives_a_core_reload(client, monkeypatch, tmp_path):
    before = JobStore(root=str(tmp_path))
    for name in "abc":
        before.save_worker(_worker(name))
    monkeypatch.setattr(tasks, "store", before)
    monkeypatch.setattr(tasks, "_claim_task", lambda payload: {"task": "t"})
    assert client.post("/api/tasks/claim", json={"node_name": "a", "vram_mb": 0}).json()["task"] == "t"

    # A restarted CORE builds a new store from the same repository.
    monkeypatch.setattr(tasks, "store", JobStore(root=str(tmp_path)))
    turns._yielding_since.clear()

    again = client.post("/api/tasks/claim", json={"node_name": "a", "vram_mb": 0}).json()
    assert again == {"task": None, "worker_state": "WAITING_FOR_TURN"}
    assert client.post("/api/tasks/claim", json={"node_name": "b", "vram_mb": 0}).json()["task"] == "t"
