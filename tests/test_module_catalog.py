"""i.0.0.4.96 (#496) AC2/AC4/AC5: module x node choice with honest compatibility."""

import json

import pytest


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.app import app
    from core.state import store
    from core.workflow.runtime import WorkflowRuntime
    dag = tmp_path / "dag"
    dag.mkdir()
    (dag / "c.json").write_text(json.dumps({"name": "C", "version": "1.0.0", "tasks": [{"id": "a", "type": "text_generation"}]}))
    for name, value in {"VERTEP_MODULE_LIFECYCLE_ROOT": str(tmp_path / "lc"), "VERTEP_MODULE_PLACEMENT_FILE": str(tmp_path / "p.json"),
                        "WORKFLOW_STATE_ROOT": str(tmp_path / "s"), "VERTEP_MODULE_AUDIT_FILE": str(tmp_path / "a.jsonl")}.items():
        monkeypatch.setenv(name, value)
    runtime = WorkflowRuntime(dag)
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: runtime)
    nodes = {"cpu-node": {"role": "gpu", "gpu_available": False, "vram_mb": 0, "cpu_cores": 4, "ram_mb": 8192},
             "gpu-node": {"role": "gpu", "gpu_available": True, "vram_mb": 12288, "cpu_cores": 8, "ram_mb": 32768},
             "text-node": {"role": "text", "gpu_available": False, "vram_mb": 0, "cpu_cores": 4, "ram_mb": 8192}}
    for name, facts in nodes.items():
        monkeypatch.setitem(store.workers, name, {"node_name": name, "status": "READY", **facts})
    client = TestClient(app)
    client.runtime = runtime
    return client


def entry(client, module_id):
    return next(m for m in client.get("/api/nodes/module-catalog").json()["modules"] if m["module_id"] == module_id)


def test_catalog_lists_modules_nodes_and_reasons(api):
    from core.module.lifecycle import BUNDLED

    catalog = api.get("/api/nodes/module-catalog").json()
    assert {m["module_id"] for m in catalog["modules"]} == set(BUNDLED)
    assert all(m["state"] == "NOT_INSTALLED" for m in catalog["modules"] if not m.get("system"))
    # Web UI is deployed by the release (owner decision 20.11.2026): a system component, no actions.
    web_ui = next(m for m in catalog["modules"] if m["module_id"] == "web-ui")
    assert web_ui["system"] and web_ui["state"] == "SYSTEM" and web_ui["nodes"] == []
    assert web_ui["health"] in {"RUNNING", "UNAVAILABLE"}
    image = {n["node_id"]: n["compatible_reasons"] for n in entry(api, "image")["nodes"]}
    assert image["gpu-node"] == []
    assert any("GPU" in reason for reason in image["cpu-node"])  # no GPU: honest reason, not READY
    assert any("середовище" in reason for reason in image["text-node"])
    text = {n["node_id"]: n["compatible_reasons"] for n in entry(api, "text")["nodes"]}
    assert text["text-node"] == []  # a node without GPU runs CPU modules
    assert {n["node_id"] for n in catalog["nodes"]} >= {"cpu-node", "gpu-node", "text-node"}
    assert all("environment" in n for n in catalog["nodes"])


def test_incompatible_placement_is_refused_with_the_reason_and_independent_modules_proceed(api):
    assert api.post("/api/nodes/module-registry/lifecycle/install", json={"bundled": "image"}).status_code == 200
    assert api.post("/api/nodes/module-registry/lifecycle/install", json={"bundled": "text"}).status_code == 200
    refused = api.put("/api/nodes/module-registry/placement/image", json={"node_ids": ["cpu-node"]})
    assert refused.status_code == 422 and "GPU" in refused.json()["detail"]
    assert api.put("/api/nodes/module-registry/placement/image", json={"node_ids": ["ghost"]}).status_code == 422
    assert api.put("/api/nodes/module-registry/placement/image", json={"node_ids": ["gpu-node"]}).status_code == 200
    assert api.put("/api/nodes/module-registry/placement/text", json={"node_ids": ["text-node"]}).status_code == 200
    assert entry(api, "image")["placement"] == ["gpu-node"] and entry(api, "text")["placement"] == ["text-node"]
    assert entry(api, "image")["state"] == "INSTALLED"  # nothing READY without a Worker report + activation
    assert api.runtime.modules.ready_instances("image") == []


def test_lab_needs_a_separate_nonproduction_vm_node(api):
    from core.module.catalog import compatibility
    from core.module.manifest import ModuleManifest
    lab = ModuleManifest.from_dict({"schema_version": "2.0", "name": "lab", "version": "1.0.0", "capabilities": [],
                                    "placement": {"target": "isolated_lab"}})
    node = {"node_id": "text-node", "role": "text"}
    assert any("LAB" in r for r in compatibility(lab, node, {"environment": "production", "isolation": "host"}))
    assert compatibility(lab, node, {"environment": "nonproduction", "isolation": "vm"}) == []


def test_role_access_is_unchanged_and_choice_is_admin_only(api, monkeypatch):
    import hashlib
    import hmac
    from fastapi.testclient import TestClient
    from core.app import app
    from core.security import SESSION_ROLES, _session_token, session_secret
    assert SESSION_ROLES == ("admin", "moderator")
    monkeypatch.setenv("USERS_JSON", json.dumps({"reader": {"password_hash": "x", "role": "moderator"}}))
    session = _session_token("reader", "moderator")
    secret = session_secret()
    csrf = hmac.new(secret.encode() if isinstance(secret, str) else secret, session.encode(), hashlib.sha256).hexdigest()
    moderator = TestClient(app, headers={"Authorization": ""})
    moderator.cookies.set("vertep_session", session)
    moderator.cookies.set("vertep_csrf", csrf)
    assert moderator.get("/api/nodes/module-catalog").status_code == 200  # read-only view
    assert moderator.post("/api/nodes/module-registry/lifecycle/install", json={"bundled": "text"},
                       headers={"x-csrf-token": csrf}).status_code == 403
    assert moderator.put("/api/nodes/module-registry/placement/text", json={"node_ids": ["text-node"]},
                      headers={"x-csrf-token": csrf}).status_code == 403



def test_system_components_refuse_lifecycle_actions_and_official_permissions_can_be_approved_before_install(api):
    # Web UI is deployed and updated with the release: no install/activate/uninstall.
    for action, body in (("install", {"bundled": "web-ui"}), ("activate", {"module_id": "web-ui"}),
                         ("uninstall", {"module_id": "web-ui", "confirm": True})):
        response = api.post(f"/api/nodes/module-registry/lifecycle/{action}", json=body)
        assert response.status_code == 409, (action, response.text)
    # The administrator could press "approve permissions" on a not yet installed official
    # module and nothing happened (400 shown out of view): the release manifest declares them.
    assert entry(api, "monitoring")["state"] == "NOT_INSTALLED"
    response = api.put("/api/nodes/module-registry/approval/monitoring", json={"permissions": ["monitoring.read"]})
    assert response.status_code == 200, response.text
    assert entry(api, "monitoring")["approved_permissions"] == ["monitoring.read"]
    assert api.put("/api/nodes/module-registry/approval/monitoring",
                   json={"permissions": ["monitoring.write"]}).status_code in {400, 422}
