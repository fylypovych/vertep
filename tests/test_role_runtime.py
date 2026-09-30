"""Issue #80: role contracts must expose a measured runtime status.

The role catalog is a static declaration; ``runtime_status`` must be derived
from real evidence for every role (including ``core``) and must be surfaced in
the role surface used by both the Web UI and dispatch.
"""
import json
from pathlib import Path

import pytest

from core import role_runtime


ROLES = {
    "core": {"label": "Core Node", "capabilities": ["scheduling"],
             "modules": ["core", "api"], "services": ["core", "postgres"]},
    "gpu": {"label": "GPU Node", "capabilities": ["image_generation"],
            "modules": ["worker", "comfyui"], "services": ["worker", "comfyui"]},
    "text": {"label": "Text Node", "capabilities": ["text_generation"],
             "modules": ["ollama"], "services": ["worker", "ollama"]},
    "voice": {"label": "Voice Node", "capabilities": ["speech_synthesis"],
              "modules": ["tts_runtime"], "services": ["worker", "tts"]},
    "publisher": {"label": "Publisher Node", "capabilities": ["publishing"],
                  "modules": ["publisher_worker"], "services": ["worker", "publisher-worker"]},
    "backup": {"label": "Backup Node", "capabilities": ["backup"],
               "modules": ["backup_service"], "services": ["worker", "backup-service"]},
    "monitoring": {"label": "Monitoring Node", "capabilities": ["metrics"],
                   "modules": ["grafana"], "services": ["worker", "monitoring"]},
}


@pytest.fixture
def roles_file(tmp_path, monkeypatch):
    path = tmp_path / "node_roles.json"
    path.write_text(json.dumps(ROLES), encoding="utf-8")
    monkeypatch.setenv("NODE_ROLES_FILE", str(path))
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path / "state"))
    return path


def _node(role, node_id, runtime_status, revoked=False):
    return {"node_id": node_id, "role": role, "status": "REVOKED" if revoked else "READY",
            "runtime_status": runtime_status, "capabilities": ROLES[role]["capabilities"],
            "revoked_at": "2026-09-29T00:00:00+00:00" if revoked else None,
            "last_self_test_at": "2026-09-29T00:00:00+00:00"}


def test_every_declared_role_has_a_runtime_status(roles_file, monkeypatch):
    monkeypatch.setattr(role_runtime, "_remote_status",
                        lambda role: ("OFFLINE", [{"node_id": f"{role}-01", "live": False}]))
    matrix = role_runtime.role_runtime_matrix()
    assert {item["role"] for item in matrix} == set(ROLES)
    for item in matrix:
        assert item["runtime_status"] in role_runtime.ROLE_RUNTIME_STATUSES


def test_core_role_is_measured_from_local_health(roles_file, monkeypatch):
    monkeypatch.setenv("NODE_ROLE", "core")
    monkeypatch.setattr(role_runtime, "_local_evidence",
                        lambda: {"checked_at": "2026-09-29T00:00:00+00:00", "checks_ok": True,
                                 "failed_checks": [], "services_total": 2,
                                 "services_missing": [], "services_unhealthy": []})
    measured = role_runtime.role_runtime_status("core")
    assert measured["runtime_status"] == "READY"
    assert measured["evidence"]["source"] == "local"


def test_local_role_with_missing_services_is_degraded(roles_file, monkeypatch):
    monkeypatch.setenv("NODE_ROLE", "gpu")
    monkeypatch.setattr(role_runtime, "_local_evidence",
                        lambda: {"checks_ok": True, "failed_checks": [], "services_total": 2,
                                 "services_missing": ["comfyui"], "services_unhealthy": []})
    assert role_runtime.role_runtime_status("gpu")["runtime_status"] == "DEGRADED"


def test_local_role_without_evidence_is_unknown(roles_file, monkeypatch):
    monkeypatch.setenv("NODE_ROLE", "voice")
    monkeypatch.setattr(role_runtime, "_local_evidence", lambda: None)
    assert role_runtime.role_runtime_status("voice")["runtime_status"] == "UNKNOWN"


def test_role_status_fails_closed_without_evidence(roles_file, monkeypatch):
    monkeypatch.setenv("NODE_ROLE", "core")
    monkeypatch.setattr(role_runtime, "registered_nodes",
                        lambda: (_ for _ in ()).throw(RuntimeError("registry down")))
    measured = role_runtime.role_runtime_status("publisher")
    assert measured["runtime_status"] == "UNKNOWN"
    assert measured["evidence"]["nodes"] == []


def test_remote_role_aggregates_self_test_and_heartbeat(roles_file, monkeypatch):
    """A role is READY only when a node both self-tested and heartbeats."""
    monkeypatch.setattr(role_runtime, "registered_nodes",
                        lambda: [_node("gpu", "gpu-01", "ONLINE"),
                                 _node("gpu", "gpu-02", "PENDING_SELF_TEST")])
    monkeypatch.setattr(role_runtime.store, "load_workers", lambda: [
        {"node_id": "gpu-01", "status": "READY",
         "last_seen": role_runtime.datetime.now(role_runtime.timezone.utc).isoformat()},
        {"node_id": "gpu-02", "status": "READY",
         "last_seen": role_runtime.datetime.now(role_runtime.timezone.utc).isoformat()},
    ], raising=False)
    status, nodes = role_runtime._remote_status("gpu")
    assert status == "READY"
    assert {item["node_id"] for item in nodes} == {"gpu-01", "gpu-02"}
    assert [item for item in nodes if item["node_id"] == "gpu-01"][0]["live"] is True
    assert [item for item in nodes if item["node_id"] == "gpu-02"][0]["live"] is False


def test_remote_role_with_only_revoked_nodes_is_offline(roles_file, monkeypatch):
    monkeypatch.setattr(role_runtime, "registered_nodes",
                        lambda: [_node("backup", "backup-01", "ONLINE", revoked=True)])
    status, nodes = role_runtime._remote_status("backup")
    assert status == "OFFLINE"
    assert nodes[0]["runtime_status"] == "REVOKED"


def test_remote_role_without_nodes_is_unknown(roles_file, monkeypatch):
    monkeypatch.setattr(role_runtime, "registered_nodes", lambda: [])
    assert role_runtime._remote_status("monitoring")[0] == "UNKNOWN"


def test_stale_heartbeat_makes_a_self_tested_node_not_live(roles_file, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_TIMEOUT", "45")
    monkeypatch.setattr(role_runtime, "registered_nodes",
                        lambda: [_node("text", "text-01", "ONLINE")])
    monkeypatch.setattr(role_runtime.store, "load_workers", lambda: [
        {"node_id": "text-01", "status": "READY", "last_seen": "2020-01-01T00:00:00+00:00"},
    ], raising=False)
    status, nodes = role_runtime._remote_status("text")
    assert status == "DEGRADED"
    assert nodes[0]["live"] is False
    assert nodes[0]["heartbeat"] == "OFFLINE"


def test_dispatch_blocks_locally_deployed_broken_role(roles_file, monkeypatch, tmp_path):
    from core.dispatcher import role_runtime_blocks
    from core.first_run import config_root

    plan_path = Path(config_root()) / "deployment-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({"role": "core", "additional_roles": ["gpu"]}), encoding="utf-8")
    monkeypatch.setattr(role_runtime, "_local_evidence",
                        lambda: {"checks_ok": True, "failed_checks": [], "services_total": 2,
                                 "services_missing": ["comfyui"], "services_unhealthy": []})
    monkeypatch.setattr(role_runtime, "registered_nodes",
                        lambda: [_node("gpu", "gpu-01", "OFFLINE")])
    monkeypatch.setenv("NODE_ROLE", "core")
    assert role_runtime_blocks("gpu") is True
    assert role_runtime_blocks({"role": "gpu"}) is True
    # A role this installation does not run is never blocked by the role gate.
    assert role_runtime_blocks("voice") is False
    assert role_runtime_blocks("") is False
    assert role_runtime_blocks({}) is False


def test_dispatch_does_not_block_an_unmeasured_role(roles_file, monkeypatch):
    from core.dispatcher import role_runtime_blocks
    from core.first_run import config_root

    plan_path = Path(config_root()) / "deployment-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({"role": "core", "additional_roles": ["text"]}), encoding="utf-8")
    monkeypatch.setenv("NODE_ROLE", "core")
    monkeypatch.setattr(role_runtime, "_local_evidence", lambda: None)
    # Fail-open: the per-node self-test guard stays the authoritative gate.
    assert role_runtime_blocks("text") is False


def test_system_roles_endpoint_exposes_runtime_status(roles_file, monkeypatch):
    from core.api.system import local_roles_status

    monkeypatch.setattr(role_runtime, "registered_nodes", lambda: [])
    payload = local_roles_status()
    for role in payload["roles"]:
        assert role["runtime_status"] in role_runtime.ROLE_RUNTIME_STATUSES


def test_app_roles_endpoint_carries_runtime_status(roles_file, monkeypatch, tmp_path):
    """The role surface consumed by the Web UI must carry a measured status."""
    from core import app as core_app

    monkeypatch.setattr(role_runtime, "registered_nodes", lambda: [])
    monkeypatch.setenv("NODE_ROLE", "core")
    monkeypatch.setattr(role_runtime, "_local_evidence", lambda: None)
    config = tmp_path / "app-config"
    config.mkdir()
    (config / "installation.json").write_text(
        json.dumps({"completed_at": "2026-01-01T00:00:00Z", "node_role": "core"}), encoding="utf-8")
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    payload = core_app.local_roles_status()
    assert set(payload["role_runtime_status"]) == set(ROLES)
    for entry in payload["available_roles"]:
        assert entry["runtime_status"] in role_runtime.ROLE_RUNTIME_STATUSES
        assert "runtime_evidence" in entry
        assert "modules" in entry


def test_nodes_api_reports_measured_runtime_status(roles_file, monkeypatch):
    """`/api/nodes` must carry the measured self-test status, not just a heartbeat."""
    from core.api import nodes as nodes_api

    monkeypatch.setattr(nodes_api, "registered_nodes",
                        lambda: [dict(_node("gpu", "gpu-01", "PENDING_SELF_TEST"), role="gpu")])
    monkeypatch.setattr(nodes_api, "node_roles", lambda: ROLES)
    listing = nodes_api.nodes()
    assert len(listing) == 1
    assert listing[0]["runtime_status"] == "PENDING_SELF_TEST"
    assert listing[0]["self_test_capabilities"] == []
    assert listing[0]["last_self_test_at"] == "2026-09-29T00:00:00+00:00"

    # A live heartbeat must not upgrade a node that never self-tested.
    monkeypatch.setattr(nodes_api, "workers", lambda: [
        {"node_name": "gpu-01", "node_id": "gpu-01", "status": "READY",
         "last_seen": role_runtime.datetime.now(role_runtime.timezone.utc).isoformat(),
         "runtime_status": "ONLINE"}])
    context = nodes_api._node_context({**listing[0], "runtime": {"status": "READY"}})
    assert context["runtime_status"] == "PENDING_SELF_TEST"
