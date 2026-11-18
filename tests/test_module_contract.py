"""i.0.0.4.92 (#492): node / module definition / instance contracts.

Covers schema negotiation and legacy compatibility, several instances on
several nodes, admin placement, LAB placement, readiness negatives
(self-test, CORE compatibility, dependencies, interfaces, conflicts) and a
single owner per dispatched task through the real WorkflowRuntime.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from core.module.instances import ModuleInstanceRegistry, PlacementStore
from core.module.manifest import ModuleManifest, negotiate_schema_version
from core.module.registry import ModuleRegistry

ROOT = Path(__file__).resolve().parents[1]


def item(name, version="1.0.0", ready=True, **extra):
    return {"name": name, "version": version, "capabilities": extra.pop("capabilities", [f"{name}_cap"]),
            "dependencies": extra.pop("dependencies", []), "resource_requirements": extra.pop("resource_requirements", {}),
            "ready": ready, **extra}


CONTAINER = {"image": "registry.example/vertep/app@sha256:" + "a" * 64, "port": 8080}


def v2(name, **extra):
    if extra.get("kind") == "application":
        extra.setdefault("container", CONTAINER)
        extra.setdefault("endpoint", "http://127.0.0.1:18080")
    return item(name, schema_version="2.0", **extra)


def registry(tmp_path, core="0.0.3.88"):
    return ModuleInstanceRegistry({"core_runtime": core}, PlacementStore(tmp_path / "placement.json"))


def states(reg):
    return {(i.node_id, i.module_id): (i.state, tuple(i.reasons)) for i in reg.instances()}


# -- AC1 / AC4: schema and legacy compatibility --------------------------------

def test_legacy_manifest_and_bundled_yaml_are_read_unchanged():
    legacy = {"name": "x", "version": "0.0.2.74", "capabilities": ["a"], "dependencies": [], "resource_requirements": {}, "description": ""}
    manifest = ModuleManifest.from_dict({"manifest": legacy})
    assert manifest.schema_version == "1.0" and manifest.module_id == "x" and manifest.kind == "execution"
    assert manifest.to_dict() == legacy  # legacy consumers see the identical shape
    for path in sorted((ROOT / "modules").glob("*/manifest.yaml")):
        bundled = ModuleManifest.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert bundled.placement["scaling"] == ("single_owner" if bundled.kind == "application" else "replicated")
        assert bundled.resource_requirements.keys() == {"min_vram_mb", "min_cpu_cores", "min_ram_mb"}


def test_schema_negotiation_rejects_unknown_major_and_extended_fields_on_v1():
    assert negotiate_schema_version(None) == "1.0"
    assert negotiate_schema_version("2.7") == "2.7"  # minor is forward compatible
    with pytest.raises(ValueError, match="Unsupported"):
        negotiate_schema_version("3.0")
    with pytest.raises(ValueError, match="schema_version 2"):
        ModuleManifest.from_dict(item("x", kind="application"))


def test_v2_manifest_declares_full_contract_and_round_trips():
    data = v2("brands", module_id="vertep.brands", kind="application", core_compatibility={"min": "0.0.3.0"},
              provides=[{"type": "api", "name": "brands", "version": "1.2"}],
              requires=[{"type": "event", "name": "characters.changed", "min_version": "1.0", "max_version": "1.9"}],
              api={"namespace": "brands", "version": "1.2", "routes": ["/brands", "/brands/{id}"]},
              ui={"entry": "ui/index.js", "navigation": [{"id": "brands", "label": "Бренди"}]},
              permissions=["brands.read", "brands.write"], secret_refs=[{"name": "token", "ref": "secret://vertep.brands/token"}],
              migrations=[{"id": "m001", "version": "1.0.0"}], services=[{"name": "api"}],
              health={"self_test": "selftest", "timeout_seconds": 20})
    manifest = ModuleManifest.from_dict(data)
    assert manifest.placement == {"target": "any", "scaling": "single_owner", "requires_gpu": False}
    again = ModuleManifest.from_dict(manifest.to_dict())
    assert again.to_dict() == manifest.to_dict()
    assert manifest.core_compatible("0.0.3.88") and not manifest.core_compatible("0.0.2.99")


@pytest.mark.parametrize("bad", [
    {"secret_refs": [{"name": "t", "ref": "secret://a/b", "value": "plaintext"}]},
    {"secret_refs": [{"name": "t", "ref": "env://TOKEN"}]},
    {"kind": "application", "placement": {"scaling": "replicated"}, "container": CONTAINER},
    {"kind": "application", "container": None},  # application code always needs its own container
    {"kind": "application", "container": {"image": "registry.example/app:latest"}},  # not digest-pinned
    {"container": CONTAINER},  # execution modules run through Worker
    {"secret_refs": [{"name": "t", "ref": "secret://other/token"}]},  # foreign secret scope
    {"placement": {"target": "production_lab"}},
    {"api": {"namespace": "x", "version": "1", "routes": ["/a"]}},
    {"api": {"namespace": "x", "version": "1.0", "routes": ["/../core"]}},
    {"ui": {"entry": "/etc/passwd"}},
    {"migrations": [{"id": "m1", "version": "1.0.0"}, {"id": "m1", "version": "1.0.1"}]},
    {"permissions": ["Admin All"]},
])
def test_v2_manifest_negatives(bad):
    with pytest.raises(ValueError):
        ModuleManifest.from_dict(v2("x", **bad))


# -- AC2: node / definition / instance ------------------------------------------

def test_instances_on_several_nodes_do_not_overwrite_each_other(tmp_path):
    reg = registry(tmp_path)
    reg.report_node("gpu-a", [item("image", "1.0.0")])
    reg.report_node("gpu-b", [item("image", "1.1.0")])
    assert set(reg.dispatch_targets("image")) == {"gpu-a", "gpu-b"}
    assert reg.dispatch_targets("image")["gpu-a"]["version"] == "1.0.0"
    reg.report_node("gpu-b", [])  # node b removed its instance
    assert set(reg.dispatch_targets("image")) == {"gpu-a"}
    snapshot = reg.snapshot()
    assert set(snapshot["definitions"]) == {"image@1.0.0"}
    with pytest.raises(ValueError, match="Duplicate"):
        reg.report_node("gpu-a", [item("image"), item("image", "2.0.0")])
    assert set(reg.dispatch_targets("image")) == {"gpu-a"}  # failed report changed nothing


def test_single_owner_application_has_explicit_ownership_boundary(tmp_path):
    reg = registry(tmp_path)
    app = dict(kind="application", api={"namespace": "brands", "version": "1.0", "routes": ["/brands"]})
    reg.report_node("a", [v2("brands", **app)])
    reg.report_node("b", [v2("brands", **app)])
    assert reg.dispatch_targets("brands") == {}  # no implicit placement
    assert "placement_required" in states(reg)[("a", "brands")][1]
    with pytest.raises(ValueError, match="exactly one"):
        reg.set_placement("brands", ["a", "b"])
    reg.set_placement("brands", ["a"])
    assert set(reg.dispatch_targets("brands")) == {"a"}
    assert states(reg)[("b", "brands")] == ("NOT_READY", ("not_placed_on_node",))
    with pytest.raises(ValueError, match="deactivate"):
        reg.set_placement("brands", ["b"])  # live service is not moved implicitly
    reg.report_node("a", [v2("brands", ready=False, **app)])  # owner deactivated through lifecycle
    reg.set_placement("brands", ["b"])
    assert set(reg.dispatch_targets("brands")) == {"b"}
    # Placement survives a CORE restart.
    assert registry(tmp_path).placement.placement == {"brands": ["b"]}


# -- AC3 / AC4: readiness negatives ------------------------------------------------

def test_readiness_requires_self_test_core_compatibility_and_placement(tmp_path):
    reg = registry(tmp_path, core="0.0.3.88")
    reg.report_node("n", [item("untested", ready=False), v2("future", core_compatibility={"min": "0.0.4.0"}), item("ok")])
    current = states(reg)
    assert current[("n", "untested")] == ("NOT_READY", ("self_test_not_passed",))
    assert current[("n", "future")] == ("NOT_READY", ("core_incompatible",))
    assert current[("n", "ok")][0] == "READY"
    assert reg.node_capabilities("n") == {"ok_cap"}  # unverified capability is not offered
    reg.set_placement("ok", ["other"])
    assert reg.dispatch_targets("ok") == {}


def test_dependencies_versions_cycles_interfaces_and_conflicts_are_rejected(tmp_path):
    reg = registry(tmp_path)
    reg.report_node("n", [
        item("base", "1.0.0"),
        item("needs_new", dependencies=[{"name": "base", "min_version": "2.0.0"}]),
        item("needs_ok", dependencies=[{"name": "base", "min_version": "1.0.0", "max_version": "1.9.9"}]),
        item("cyc_a", dependencies=["cyc_b"]), item("cyc_b", dependencies=["cyc_a"]),
        v2("events", provides=[{"type": "event", "name": "jobs.done", "version": "1.4"}]),
        v2("consumer", requires=[{"type": "event", "name": "jobs.done", "min_version": "2.0"}]),
        v2("consumer_ok", requires=[{"type": "event", "name": "jobs.done", "min_version": "1.0", "max_version": "1.9"}]),
        v2("dup1", api={"namespace": "shared", "version": "1.0", "routes": []}),
        v2("dup2", api={"namespace": "shared", "version": "1.0", "routes": []}),
        item("chain", dependencies=["needs_new"]),
    ])
    current = states(reg)
    ready = {module for (_, module), (state, _) in current.items() if state == "READY"}
    assert ready == {"base", "needs_ok", "events", "consumer_ok"}
    assert "dependency_unmet:base" in current[("n", "needs_new")][1]
    assert "dependency_cycle" in current[("n", "cyc_a")][1]
    assert "interface_unmet:event:jobs.done" in current[("n", "consumer")][1]
    assert "interface_conflict" in current[("n", "dup1")][1] and "interface_conflict" in current[("n", "dup2")][1]
    assert "dependency_unmet:needs_new" in current[("n", "chain")][1]  # transitive demotion
    assert reg.conflicts() == ["api:shared claimed by dup1 and dup2"] or reg.conflicts() == ["api:shared claimed by dup2 and dup1"]


def test_local_module_registry_rejects_conflicts_interfaces_and_core_range():
    reg = ModuleRegistry({"core_runtime": "0.0.3.88"})
    provider = ModuleManifest.from_dict(v2("p", provides=[{"type": "api", "name": "x", "version": "1.0"}]))
    assert reg.register_module(provider)
    assert not reg.register_module(ModuleManifest.from_dict(v2("q", provides=[{"type": "api", "name": "x", "version": "2.0"}])))
    assert not reg.register_module(ModuleManifest.from_dict(v2("r", requires=[{"type": "api", "name": "x", "min_version": "1.1"}])))
    assert reg.register_module(ModuleManifest.from_dict(v2("s", requires=[{"type": "api", "name": "x", "min_version": "1.0"}])))
    assert not reg.unregister_module("p")  # active interface consumer is not broken silently
    assert not reg.register_module(ModuleManifest.from_dict(v2("t", core_compatibility={"max": "0.0.3.0"})))


# -- AC5: LAB placement ------------------------------------------------------------

def test_lab_module_only_on_separate_nonproduction_vm_node(tmp_path):
    reg = registry(tmp_path)
    lab = v2("lab", kind="application", placement={"target": "isolated_lab"})
    reg.report_node("prod", [lab])
    reg.report_node("labvm", [lab])
    assert reg.dispatch_targets("lab") == {}  # never implicitly placed
    with pytest.raises(ValueError, match="nonproduction VM"):
        reg.set_placement("lab", ["prod"])
    with pytest.raises(ValueError, match="nonproduction VM"):
        reg.set_placement("lab", ["labvm"])  # default profile is production/host
    with pytest.raises(ValueError):
        reg.set_node_profile("labvm", "lab", "vm")  # no 'lab' role/environment
    reg.set_node_profile("labvm", "nonproduction", "vm")
    reg.set_placement("lab", ["labvm"])
    assert set(reg.dispatch_targets("lab")) == {"labvm"}
    # Reclassifying the node back to production invalidates the placement.
    reg.set_node_profile("labvm", "production", "vm")
    assert reg.dispatch_targets("lab") == {}


# -- AC3 / AC6: integration through WorkflowRuntime and Dispatcher -----------------

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from core.state import store
    from core.workflow.runtime import WorkflowRuntime
    root = tmp_path / "dag"
    root.mkdir()
    (root / "module.json").write_text(json.dumps({"name": "Module", "tasks": [{"id": "render", "type": "custom", "module": "imagemod"}]}))
    monkeypatch.setenv("WORKFLOW_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("VERTEP_MODULE_PLACEMENT_FILE", str(tmp_path / "placement.json"))
    instance = WorkflowRuntime(root)
    monkeypatch.setattr("core.system_state.dispatch_allowed", lambda: True)
    yield instance, store
    for name in ("node-a", "node-b"):
        store.workers.pop(name, None)


def worker(name, inventory):
    now = datetime.now(timezone.utc).isoformat()
    return {"node_name": name, "role": "gpu", "status": "READY", "last_seen": now, "cpu_cores": 4, "ram_mb": 8192,
            "vram_mb": 8192, "capabilities": ["image_generation"], "tested_capabilities": ["image_generation"],
            "self_test": {"status": "PASSED", "role": "gpu", "checked_at": now}, "module_inventory": inventory}


def test_runtime_dispatches_one_owner_only_to_placed_ready_instances(runtime):
    instance, store = runtime
    a = worker("node-a", [item("imagemod", "1.0.0")])
    b = worker("node-b", [item("imagemod", "1.1.0")])
    for record in (a, b):
        store.workers[record["node_name"]] = record
    transports = {record["node_name"]: instance.register(record) for record in (a, b)}
    # Registering b did not erase a's instance.
    assert set(instance.modules.dispatch_targets("imagemod")) == {"node-a", "node-b"}

    instance.modules.set_placement("imagemod", ["node-b"])
    job = instance.create("module", {})
    instance.tick()
    assert transports["node-a"].poll() is None  # not placed there
    task = transports["node-b"].poll()
    assert task["module_name"] == "imagemod" and task["module_version"] == "1.1.0"
    assert "module_targets" not in task
    instance.tick()
    assert transports["node-a"].poll() is None and transports["node-b"].poll() is None  # one owner
    assert instance.engine._jobs[job]["tasks"]["render"]["worker_id"] == "node-b"
    transports["node-b"].result(task["lease_id"], {"ok": True})
    instance.tick()
    assert instance.engine.get_job_status(job)["status"] == "COMPLETED"


def test_runtime_withholds_untested_module_but_keeps_worker(runtime):
    instance, store = runtime
    record = worker("node-a", [item("imagemod", ready=False)])
    store.workers["node-a"] = record
    transport = instance.register(record)
    assert instance.dispatcher.query_worker_status("node-a")["capabilities"] == {"image_generation"}
    job = instance.create("module", {})
    instance.tick()
    assert transport.poll() is None
    status = instance.engine.get_job_status(job)["tasks"]["render"]
    assert status["status"] == "PENDING" and "not discovered" in status["error"]


def test_placement_api_is_admin_only_and_validates(runtime, monkeypatch):
    from fastapi.testclient import TestClient
    import hashlib
    import hmac
    from core.app import app
    from core.security import _session_token
    instance, store = runtime
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: instance)
    record = worker("node-a", [item("imagemod")])
    store.workers["node-a"] = record
    instance.register(record)
    client = TestClient(app)
    view = client.get("/api/nodes/module-registry")
    assert view.status_code == 200 and view.json()["instances"][0]["node_id"] == "node-a"
    assert client.put("/api/nodes/module-registry/placement/missing", json={"node_ids": ["node-a"]}).status_code == 422
    ok = client.put("/api/nodes/module-registry/placement/imagemod", json={"node_ids": ["node-a"]})
    assert ok.status_code == 200 and ok.json()["node_ids"] == ["node-a"]
    monkeypatch.setenv("ADMIN_PASSWORD", "rbac-fallback-password")
    monkeypatch.delenv("CONFIG_ROOT", raising=False)
    monkeypatch.setenv("USERS_JSON", json.dumps({"reader": {"password_hash": "x", "role": "viewer"}}))
    token = _session_token("reader", "viewer")
    csrf = hmac.new(b"rbac-fallback-password", token.encode(), hashlib.sha256).hexdigest()
    viewer = TestClient(app, headers={"Authorization": ""})
    denied = viewer.put("/api/nodes/module-registry/placement/imagemod", json={"node_ids": []},
                        cookies={"vertep_session": token, "vertep_csrf": csrf}, headers={"x-csrf-token": csrf})
    assert denied.status_code == 403
    assert instance.modules.placement.placement == {"imagemod": ["node-a"]}
