"""Integration harness for Fleet inventory, Worker metrics & remote controls (issue i.0.0.0.24).

Tests the observable Fleet/admin API layer directly (no worker TLS/enrollment
dependencies): remote controls' state transitions and error/ack, offline node
retention in the fleet, and preservation of stale/null metrics. Workers are seeded
through the in-process store (cleared by ``conftest.reset_in_process_api_state``
between tests), and lifecycle transitions are exercised over the exact endpoints
the Web UI uses.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.state import store


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _add_worker(nid: str, status="READY", **overrides) -> dict:
    """Seed a worker of record in the in-process store (registry + runtime)."""
    worker = {
        "node_id": nid, "node_name": nid, "status": status, "role": "gpu",
        "last_seen": _now_iso(), "gpu_name": "demo", "vram_mb": 8192,
        "free_vram_mb": 7000, "gpu_load": 10.0, "cpu_load": 0.1, "temperature": 45.0,
        "ram_mb": 16384, "disk_free_mb": 102400, "capabilities": ["image_generation"],
        "tested_capabilities": ["image_generation"], "version": "0.0.1.0",
        "runtime_version": "3.12", "current_job": None, "current_task": None,
        "self_test": {"status": "PASSED", "role": "gpu", "capabilities": ["image_generation"]},
    }
    worker.update(overrides)
    store.workers[nid] = worker
    store.save_worker(worker)
    return worker


# ── Fleet listing & detail ─────────────────────────────────────

def test_worker_appears_in_fleet(client):
    _add_worker("fleet-01")
    workers = client.get("/api/workers").json()
    ids = [w["node_id"] for w in workers]
    assert "fleet-01" in ids

def test_offline_worker_reported_as_offline(client):
    _add_worker("offline-01", last_seen="2000-01-01T00:00:00+00:00")
    workers = client.get("/api/workers").json()
    node = next(w for w in workers if w["node_id"] == "offline-01")
    assert node["status"] == "OFFLINE"

def test_worker_detail_roundtrip(client):
    _add_worker("detail-01", gpu_name="RTX Demo", vram_mb=12000)
    d = client.get("/api/nodes/detail-01").json()
    assert d["gpu_name"] == "RTX Demo"
    assert d["vram_mb"] == 12000
    assert d["status"] == "READY"

def test_null_metrics_preserved(client):
    _add_worker("nm-01", gpu_load=None, temperature=None, free_vram_mb=None)
    d = client.get("/api/nodes/nm-01").json()
    assert d.get("gpu_load") is None
    assert d.get("temperature") is None
    assert d.get("free_vram_mb") is None

# ── Remote controls: acknowledgments & transitions ─────────────

def _control(client, nid, action):
    return client.post(f"/api/nodes/{nid}/actions", json={"action": action})

def test_drain_ack_and_resume(client):
    _add_worker("drn-01")
    resp = _control(client, "drn-01", "drain")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("DRAINING", "READY")
    resp2 = _control(client, "drn-01", "resume")
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "READY"

def test_quarantine_unquarantine(client):
    _add_worker("qr-01")
    _control(client, "qr-01", "quarantine")
    assert client.get("/api/nodes/qr-01").json()["status"] == "QUARANTINED"
    _control(client, "qr-01", "unquarantine")
    assert client.get("/api/nodes/qr-01").json()["status"] == "READY"

def test_disable_enable(client):
    _add_worker("dis-01")
    _control(client, "dis-01", "disable")
    d = client.get("/api/nodes/dis-01").json()
    assert d["status"] == "OFFLINE"
    _control(client, "dis-01", "enable")
    assert client.get("/api/nodes/dis-01").json()["status"] in ("READY", "ERROR")

def test_self_test_ack(client):
    _add_worker("st-01")
    resp = _control(client, "st-01", "self-test")
    assert resp.status_code == 200
    assert resp.json()["status"] == "SELF_TESTING"

def test_restart_action_updates_desired_state(client):
    _add_worker("rst-01")
    resp = _control(client, "rst-01", "restart")
    assert resp.status_code == 200
    d = client.get("/api/nodes/rst-01").json()
    assert d["status"] == "UPDATING"
    assert d["update_state"].get("desired_state") == "RESTARTING"

def test_update_action_updates_desired_state(client):
    _add_worker("upd-01")
    resp = _control(client, "upd-01", "update")
    assert resp.status_code == 200
    d = client.get("/api/nodes/upd-01").json()
    assert d["update_state"].get("desired_state") == "UPDATING"

def test_revoke_ack(client):
    _add_worker("rev-01")
    resp = client.post("/api/nodes/rev-01/revoke")
    assert resp.status_code == 200

def test_unknown_node_action_404(client):
    assert client.post("/api/nodes/no-such/actions", json={"action": "drain"}).status_code == 404

def test_invalid_action_rejected(client):
    _add_worker("inv-01")
    assert client.post("/api/nodes/inv-01/actions", json={"action": "bogus"}).status_code in (400, 422)

def test_busy_worker_rejects_self_test(client):
    _add_worker("bst-01", status="BUSY", current_task="task-x")
    assert client.post("/api/nodes/bst-01/actions", json={"action": "self-test"}).status_code == 409

# ── State transition machine ───────────────────────────────────

def test_ready_busy_ready_cycle(client):
    _add_worker("tc-01")
    store.workers["tc-01"]["status"] = "BUSY"
    store.workers["tc-01"]["current_task"] = "t-1"
    store.save_worker(store.workers["tc-01"])
    assert client.get("/api/nodes/tc-01").json()["status"] == "BUSY"
    store.workers["tc-01"].update({"status": "READY", "current_task": None})
    store.save_worker(store.workers["tc-01"])
    assert client.get("/api/nodes/tc-01").json()["status"] == "READY"