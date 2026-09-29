"""Issue #78: per-node model placement, catalog cache and progress channel.

Covers the Async model pull progress/cancel та per-node placement/readiness/cache
lifecycle acceptance criteria that can be proven without a real Ollama node:
placement rejection paths, heartbeat catalog ingestion, command delivery/ack,
worker-authenticated progress reporting, cache invalidation and restart
reconciliation.
"""

import pytest
from fastapi.testclient import TestClient

from core.app import app, store
from core.operations import advance_operation, begin_operation, create_operation, get_operation
from core.pull_executor import (
    PlacementError,
    cancel_pull,
    model_placement,
    pending_model_command,
    reconcile_model_pull_operations,
    split_target,
    start_delete,
    start_pull,
)

client = TestClient(app)


def _heartbeat(node_name, **overrides):
    payload = {"node_name": node_name, "role": "text", "vram_mb": 0,
               "capabilities": ["text_generation"], "supported_tasks": ["text"]}
    payload.update(overrides)
    response = client.post("/api/workers/heartbeat", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _progress(node_name, operation_id, status="RUNNING", progress=0,
              phase="pull", error=None, token=None):
    headers = {"x-vertep-token": token} if token else {}
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
