"""A failed update does not restart the stack; a deleted Job does not poison the queue."""

import importlib.util
import json

import pytest
from fastapi.testclient import TestClient

from core.api import tasks as tasks_api
from core.app import app
from core.state import store, task_queue


@pytest.fixture
def watchdog(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("watchdog_script", "scripts/watchdog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("VERTEP_ROOT", str(tmp_path))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(module, "check_role", lambda: "core")
    restarts = []
    monkeypatch.setattr(module, "restart_containers", lambda role: restarts.append(role) or "ok")
    return module, restarts, tmp_path


@pytest.mark.parametrize("state", ["FAILED", "ROLLED_BACK"])
def test_failed_update_alone_does_not_restart(watchdog, monkeypatch, state):
    module, restarts, root = watchdog
    (root / "status.json").write_text(json.dumps({"state": state}), encoding="utf-8")
    monkeypatch.setattr(module, "check_containers", lambda role: {"core": True})
    module.main()
    assert restarts == []


def test_missing_status_file_does_not_restart(watchdog, monkeypatch):
    module, restarts, _ = watchdog
    monkeypatch.setattr(module, "check_containers", lambda role: {"core": True})
    module.main()
    assert restarts == []


def test_stopped_container_restarts_except_during_an_update(watchdog, monkeypatch):
    module, restarts, root = watchdog
    monkeypatch.setattr(module, "check_containers", lambda role: {"core": False})
    (root / "status.json").write_text(json.dumps({"state": "RUNNING"}), encoding="utf-8")
    module.main()
    assert restarts == []
    (root / "status.json").write_text(json.dumps({"state": "SUCCEEDED"}), encoding="utf-8")
    module.main()
    assert restarts == ["core"]


def test_task_of_a_deleted_job_is_dropped_and_does_not_crash_claim(monkeypatch):
    monkeypatch.setattr(tasks_api, "_valid_worker_request", lambda node, request: True)
    store.workers["n"] = {"node_name": "n", "status": "READY"}
    task = task_queue.enqueue({"job_id": "deleted-job", "task": "storyboard", "kind": "image_storyboard"})
    try:
        response = TestClient(app).post("/api/tasks/claim", json={"node_name": "n"})
        assert response.status_code == 200, response.text
        assert response.json()["task"] is None
        assert not task_queue.inflight_has(task["task_id"])
    finally:
        store.workers.pop("n", None)
