"""Issue #78: per-node model placement, catalog cache and progress channel.

Covers the Async model pull progress/cancel та per-node placement/readiness/cache
lifecycle acceptance criteria that can be proven without a real Ollama node:
placement rejection paths, heartbeat catalog ingestion, command delivery/ack,
worker-authenticated progress reporting, cache invalidation and restart
reconciliation.

Issue i.0.0.1.69: the local pull stream must reach COMPLETED only through the
explicit terminal success event, EOF without it is a failure, cancellation
aborts a stalled transport promptly and a late result never rewrites a
terminal state.
"""

import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from core.app import app, store
from core.operations import (advance_operation, begin_operation, cancel_operation,
                             complete_operation, create_operation,
                             fail_operation, get_operation)
from core.pull_executor import (
    PlacementError,
    _CANCEL_EVENTS,
    cancel_pull,
    model_placement,
    pending_model_command,
    reconcile_model_pull_operations,
    split_target,
    start_delete,
    start_pull,
)

client = TestClient(app)


# Worker endpoints require an authenticated worker token (deny-by-default since
# 0.0.2.51), matching tests/test_api.py.
_WORKER_TOKEN = "model-lifecycle-worker-token"


@pytest.fixture(autouse=True)
def _worker_token(monkeypatch):
    monkeypatch.setenv("NODE_API_TOKEN", _WORKER_TOKEN)


def _heartbeat(node_name, **overrides):
    payload = {"node_name": node_name, "role": "text", "vram_mb": 0,
               "capabilities": ["text_generation"], "supported_tasks": ["text"]}
    payload.update(overrides)
    response = client.post("/api/workers/heartbeat", json=payload,
                           headers={"x-vertep-node-name": node_name,
                                    "x-vertep-token": _WORKER_TOKEN})
    assert response.status_code == 200, response.text
    return response.json()


def _progress(node_name, operation_id, status="RUNNING", progress=0,
              phase="pull", error=None, token=None):
    headers = {"x-vertep-node-name": node_name,
               "x-vertep-token": _WORKER_TOKEN if token is None else token}
    return client.post("/api/workers/model-progress", headers=headers, json={
        "node_name": node_name, "operation_id": operation_id,
        "status": status, "phase": phase, "progress": progress, "error": error,
    })


def _placement(node_name):
    return {entry["node_name"]: entry for entry in model_placement()}.get(node_name)


# ---------------------------------------------------------------------------
# Operation progress contract
# ---------------------------------------------------------------------------


def test_advance_operation_progress_is_not_an_error():
    operation = create_operation("model_pull", "test", target="advance-demo")
    advance_operation(operation["operation_id"], "pull", 42, "downloading layers")
    stored = get_operation(operation["operation_id"])
    assert stored["progress"] == 42
    assert stored["message"] == "downloading layers"
    assert stored["error"] is None
    assert stored["status"] == "QUEUED"


def test_split_target_roundtrip():
    assert split_target("llama3:latest") == (None, "llama3:latest")
    assert split_target("text-node|llama3:latest") == ("text-node", "llama3:latest")
    assert split_target(None) == (None, "")


# ---------------------------------------------------------------------------
# Catalog ingestion and placement readiness
# ---------------------------------------------------------------------------


def test_heartbeat_model_catalog_feeds_placement():
    _heartbeat("cat-node", model_catalog={"models": ["llama3:latest", "qwen3:4b"]})
    entry = _placement("cat-node")
    assert entry["ready"] is True
    assert entry["stale"] is False
    assert entry["models"] == ["llama3:latest", "qwen3:4b"]
    assert entry["model_count"] == 2
    assert entry["pending_command"] is False
    assert entry["catalog_at"]


def test_catalog_survives_heartbeat_that_reports_nothing():
    _heartbeat("keep-cat", model_catalog={"models": ["a:1"]})
    _heartbeat("keep-cat")
    entry = _placement("keep-cat")
    assert entry["models"] == ["a:1"]
    assert entry["ready"] is True


def test_disabled_node_drops_model_cache_and_readiness():
    _heartbeat("dis-cache", model_catalog={"models": ["a:1"]})
    assert _placement("dis-cache")["ready"] is True
    store.workers["dis-cache"]["desired_state"] = "DISABLED"
    store.save_worker(store.workers["dis-cache"])
    _heartbeat("dis-cache")
    worker = store.workers["dis-cache"]
    assert "model_catalog" not in worker
    assert worker.get("desired_state") == "DISABLED"
    entry = _placement("dis-cache")
    assert entry["ready"] is False
    assert entry["stale"] is True


def test_revoked_node_is_not_a_placement_target():
    _heartbeat("rev-cache", model_catalog={"models": ["a:1"]})
    store.workers["rev-cache"]["desired_state"] = "REVOKED"
    store.save_worker(store.workers["rev-cache"])
    _heartbeat("rev-cache")
    entry = _placement("rev-cache")
    assert entry["ready"] is False
    assert entry["stale"] is True
    with pytest.raises(PlacementError) as exc:
        start_pull("revoked-model", node_name="rev-cache")
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# Placement rejection paths
# ---------------------------------------------------------------------------


def test_pull_unknown_node_returns_404():
    response = client.post("/api/system/models/pull",
                           json={"name": "ghost-model", "node": "ghost-node"})
    assert response.status_code == 404
    assert "ghost-node" in response.json()["detail"]


def test_pull_non_text_node_returns_409():
    _heartbeat("gpu-node", role="gpu", capabilities=["image_generation"],
               supported_tasks=["image"])
    response = client.post("/api/system/models/pull",
                           json={"name": "gpu-model", "node": "gpu-node"})
    assert response.status_code == 409
    assert "cannot host text models" in response.json()["detail"]


def test_pull_disabled_node_returns_409():
    _heartbeat("off-node")
    store.workers["off-node"]["desired_state"] = "DISABLED"
    store.save_worker(store.workers["off-node"])
    response = client.post("/api/system/models/pull",
                           json={"name": "m", "node": "off-node"})
    assert response.status_code == 409


def test_pull_stale_heartbeat_returns_409():
    _heartbeat("stale-node")
    from datetime import datetime, timedelta, timezone
    store.workers["stale-node"]["last_seen"] = (
        datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    store.save_worker(store.workers["stale-node"])
    with pytest.raises(PlacementError) as exc:
        start_pull("stale-model", node_name="stale-node")
    assert exc.value.status_code == 409
    assert "heartbeat timed out" in exc.value.message


def test_delete_requires_node_target():
    with pytest.raises(PlacementError) as exc:
        start_delete("orphan-model")
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Command delivery, progress and completion
# ---------------------------------------------------------------------------


def test_node_pull_command_progress_and_completion():
    _heartbeat("pull-node", model_catalog={"models": ["keep:latest"]})
    operation = start_pull("fresh-model:1", node_name="pull-node")
    assert operation["type"] == "model_pull"
    assert operation["node_name"] == "pull-node"
    assert operation["model"] == "fresh-model:1"
    assert operation["status"] == "RUNNING"

    control = _heartbeat("pull-node")
    command = control["model_command"]
    assert command["action"] == "pull"
    assert command["model"] == "fresh-model:1"
    assert command["operation_id"] == operation["operation_id"]

    response = _progress("pull-node", operation["operation_id"], "RUNNING", 35)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["cancel_requested"] is False
    assert body["operation"]["progress"] == 35
    assert body["operation"]["error"] is None

    # First progress sample acknowledges the command: no redelivery.
    assert _heartbeat("pull-node")["model_command"] is None

    response = _progress("pull-node", operation["operation_id"], "COMPLETED", 100)
    assert response.json()["operation"]["status"] == "COMPLETED"
    worker = store.workers["pull-node"]
    assert "model_command" not in worker
    assert "model_command_ack" not in worker
    assert worker["model_catalog_at"] is None

    entry = _placement("pull-node")
    assert entry["stale"] is True
    assert entry["ready"] is False
    assert entry["pending_command"] is False


def test_node_pull_failure_is_reported_durably():
    _heartbeat("fail-node")
    operation = start_pull("bad-model", node_name="fail-node")
    response = _progress("fail-node", operation["operation_id"], "FAILED",
                         10, error="no space left on device")
    assert response.status_code == 200
    stored = get_operation(operation["operation_id"])
    assert stored["status"] == "FAILED"
    assert "no space left on device" in stored["error"]
    # The node reported the terminal state itself, so no command is re-delivered.
    assert pending_model_command("fail-node") is None
    assert "model_command" not in store.workers["fail-node"]


def test_core_side_failure_reissues_cancel_to_node():
    _heartbeat("corefail-node")
    operation = start_pull("corefail-model", node_name="corefail-node")
    from core.operations import fail_operation
    fail_operation(operation["operation_id"], "operator aborted")
    assert pending_model_command("corefail-node")["action"] == "cancel"
    assert _heartbeat("corefail-node")["model_command"]["action"] == "cancel"


def test_cancel_pull_signals_node_and_marks_cancelled():
    _heartbeat("cancel-node")
    operation = start_pull("cancel-model", node_name="cancel-node")
    updated = cancel_pull(operation["operation_id"], "user cancelled")
    assert updated["status"] == "CANCELLED"
    assert pending_model_command("cancel-node")["action"] == "cancel"
    control = _heartbeat("cancel-node")
    assert control["model_command"]["action"] == "cancel"

    response = _progress("cancel-node", operation["operation_id"], "CANCELLED", 0)
    assert response.status_code == 200
    assert response.json()["cancel_requested"] is False
    assert pending_model_command("cancel-node") is None


def test_model_progress_unknown_operation_returns_404():
    _heartbeat("prog-node")
    response = _progress("prog-node", "a" * 32)
    assert response.status_code == 404


def test_model_progress_requires_worker_token(monkeypatch):
    _heartbeat("sec-node")
    operation = start_pull("sec-model", node_name="sec-node")
    monkeypatch.setenv("NODE_API_TOKEN", "topsecret")
    assert _progress("sec-node", operation["operation_id"], token="wrong").status_code == 401
    assert _progress("sec-node", operation["operation_id"],
                     token="topsecret").status_code == 200
    cancel_pull(operation["operation_id"], "cleanup")


def test_model_progress_rejects_unknown_status():
    _heartbeat("badstatus-node")
    operation = start_pull("badstatus-model", node_name="badstatus-node")
    response = client.post("/api/workers/model-progress", json={
        "node_name": "badstatus-node", "operation_id": operation["operation_id"],
        "status": "EXPLODED", "phase": "pull", "progress": 1})
    assert response.status_code == 422
    cancel_pull(operation["operation_id"], "cleanup")


def test_local_pull_without_ollama_fails_operation(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "")
    operation = start_pull("local-orphan-model")
    assert operation["node_name"] is None
    for _ in range(100):
        stored = get_operation(operation["operation_id"])
        if stored["status"] in {"FAILED", "COMPLETED", "CANCELLED"}:
            break
        import time
        time.sleep(0.02)
    stored = get_operation(operation["operation_id"])
    assert stored["status"] == "FAILED"
    assert "OLLAMA_URL" in stored["error"]


# ---------------------------------------------------------------------------
# Restart reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_fails_orphaned_local_pull():
    operation = create_operation("model_pull", "test", target="orphan-model")
    begin_operation(operation["operation_id"], "pull")
    reconcile_model_pull_operations()
    stored = get_operation(operation["operation_id"])
    assert stored["status"] == "FAILED"
    assert "CORE restart" in stored["error"]


def test_reconcile_keeps_unacknowledged_node_command():
    _heartbeat("keep-node")
    operation = start_pull("keep-model", node_name="keep-node")
    reconcile_model_pull_operations()
    stored = get_operation(operation["operation_id"])
    assert stored["status"] == "RUNNING"
    assert pending_model_command("keep-node")["action"] == "pull"


def test_reconcile_fails_acknowledged_node_command():
    _heartbeat("ack-node")
    operation = start_pull("ack-model", node_name="ack-node")
    _progress("ack-node", operation["operation_id"], "RUNNING", 5)
    reconcile_model_pull_operations()
    stored = get_operation(operation["operation_id"])
    assert stored["status"] == "FAILED"
    assert "CORE restart" in stored["error"]


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_system_models_endpoint_exposes_nodes():
    _heartbeat("api-node", model_catalog={"models": ["x:1"]})
    response = client.get("/api/system/models")
    assert response.status_code == 200
    body = response.json()
    assert "models" in body
    assert "nodes" in body
    assert "api-node" in {node["node_name"] for node in body["nodes"]}

    nodes_response = client.get("/api/system/models/nodes")
    assert nodes_response.status_code == 200
    assert "api-node" in {n["node_name"] for n in nodes_response.json()["nodes"]}


def test_model_pull_endpoint_is_idempotent_for_active_operation():
    _heartbeat("idem-node")
    first = client.post("/api/system/models/pull",
                        json={"name": "idem-model", "node": "idem-node"})
    second = client.post("/api/system/models/pull",
                         json={"name": "idem-model", "node": "idem-node"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["operation_id"] == second.json()["operation_id"]
    cancel_pull(first.json()["operation_id"], "cleanup")


# ---------------------------------------------------------------------------
# Local pull stream contract (Issue i.0.0.1.69)
# ---------------------------------------------------------------------------


class _PullStream:
    status_code = 200

    def __init__(self, lines, *, stall=False):
        self._lines = lines
        self._stall = stall
        self.started = threading.Event()
        self.closed = threading.Event()

    @property
    def text(self):
        return ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self):
        self.started.set()
        for line in self._lines:
            yield line
        if self._stall:
            # A stalled transport never yields another line: only closing the
            # response (the cancellation watcher) can release it.
            if not self.closed.wait(timeout=5):
                raise httpx.ReadError("stream stalled")
            raise httpx.ReadError("stream closed by cancellation")

    def close(self):
        self.closed.set()


class _PullClient:
    stream_obj = None

    def __init__(self, *_args, **_kwargs):
        pass

    def stream(self, *_args, **_kwargs):
        return _PullClient.stream_obj

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _start_local_pull(monkeypatch, lines, *, model, stall=False):
    stream = _PullStream(lines, stall=stall)
    _PullClient.stream_obj = stream
    monkeypatch.setattr(httpx, "Client", _PullClient)
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    operation = start_pull(model)
    assert operation["node_name"] is None
    return operation, stream


def _wait_terminal(operation_id, timeout=3.0):
    deadline = time.monotonic() + timeout
    stored = get_operation(operation_id)
    while time.monotonic() < deadline:
        stored = get_operation(operation_id)
        if stored and stored["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        time.sleep(0.02)
    return stored


def test_local_pull_requires_terminal_success_event(monkeypatch):
    operation, _stream = _start_local_pull(monkeypatch, [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"completed": 50, "total": 100}),
        json.dumps({"completed": 100, "total": 100}),
    ], model="local-eof-model")
    stored = _wait_terminal(operation["operation_id"])
    assert stored["status"] == "FAILED"
    assert "terminal success" in stored["error"]
    # the persisted progress of the interrupted pull is preserved.
    assert stored["progress"] == 100


def test_local_pull_eof_after_success_completes(monkeypatch):
    operation, _stream = _start_local_pull(monkeypatch, [
        json.dumps({"completed": 100, "total": 100}),
        json.dumps({"status": "success"}),
    ], model="local-success-model")
    stored = _wait_terminal(operation["operation_id"])
    assert stored["status"] == "COMPLETED"
    assert stored["result"]["pulled"] is True
    assert stored["progress"] == 100


def test_local_pull_error_event_fails(monkeypatch):
    operation, _stream = _start_local_pull(monkeypatch, [
        json.dumps({"error": "model not found"}),
    ], model="local-error-model")
    stored = _wait_terminal(operation["operation_id"])
    assert stored["status"] == "FAILED"
    assert "model not found" in stored["error"]


def test_local_pull_cancel_interrupts_stalled_transport(monkeypatch):
    operation, stream = _start_local_pull(monkeypatch, [
        json.dumps({"status": "pulling manifest"}),
    ], model="local-stalled-model", stall=True)
    assert stream.started.wait(timeout=2)
    cancel_pull(operation["operation_id"], "user cancelled")
    # The cancellation watcher closes the stalled transport, so the pull
    # thread finishes promptly instead of blocking until the read timeout.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and operation["operation_id"] in _CANCEL_EVENTS:
        time.sleep(0.02)
    assert operation["operation_id"] not in _CANCEL_EVENTS
    time.sleep(0.1)
    stored = get_operation(operation["operation_id"])
    # The late transport error must not rewrite the terminal CANCELLED state.
    assert stored["status"] == "CANCELLED"


def test_terminal_operation_state_is_final_for_late_results():
    operation = create_operation("model_pull", "test", target="terminal-final")
    begin_operation(operation["operation_id"], "pull")
    advance_operation(operation["operation_id"], "pull", 40, "downloading")
    cancel_operation(operation["operation_id"], "user cancelled")
    complete_operation(operation["operation_id"], {"pulled": True})
    fail_operation(operation["operation_id"], "late transport error")
    advance_operation(operation["operation_id"], "pull", 77, "late progress")
    stored = get_operation(operation["operation_id"])
    assert stored["status"] == "CANCELLED"
    assert stored["progress"] == 40
    assert stored["message"] == "downloading"
