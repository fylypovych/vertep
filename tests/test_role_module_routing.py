"""i.0.0.4.97 (#497): every migrated role function has a managed module route.

Text is proven end-to-end in tests/test_role_module_migration.py. Here, for
Image/Video, Voice, Publisher and Backup: bundled install through the common
lifecycle, CORE binding per task kind, the Worker's module path on the same
role executor, one execution owner per attempt and shadow without a second
external effect. Physical GPU parity is #441.
"""

import json

import pytest

from core.module.instances import ModuleInstanceRegistry, PlacementStore
from core.module.lifecycle import BUNDLED, ModuleLifecycle, bundled_manifest
from core.module import routing
from worker import migration


@pytest.fixture
def managed(tmp_path, monkeypatch):
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.3.90"}, PlacementStore(tmp_path / "p.json"))
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda package: (_ for _ in ()).throw(AssertionError("unsigned")),
                                registry=registry, system_versions={"core_runtime": "0.0.3.90"})

    class Runtime:
        modules = registry
    Runtime.lifecycle = lifecycle
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: Runtime)
    return lifecycle, registry


@pytest.mark.parametrize("role,module_id,kinds", [("gpu", "image", ["image", "video"]), ("voice", "audio", ["voice"]),
                                                  ("publisher", "publisher", ["publish"]), ("backup", "backup", ["backup"])])
def test_bundled_function_switches_route_through_lifecycle(managed, monkeypatch, role, module_id, kinds):
    lifecycle, registry = managed
    monkeypatch.setenv("NODE_ROLE", role)
    monkeypatch.delenv(f"VERTEP_{module_id.upper()}_MODULE_ENABLED", raising=False)
    report = migration.inventory()
    assert [item["name"] for item in report] == [module_id] and report[0]["enabled"] is False
    registry.report_node("node-1", report)
    registry.report_node("node-2", report)
    assert registry.ready_instances(module_id) == []  # unflagged + unmanaged: legacy only
    assert all(routing.binding(kind, "node-1") is None for kind in kinds)

    lifecycle.install_bundled(module_id)
    assert all(routing.binding(kind, "node-1") is None for kind in kinds)  # installed only: not switched
    registry.set_placement(module_id, ["node-1"])
    lifecycle.activate(module_id)
    for kind in kinds:
        assert routing.binding(kind, "node-1") == {"module_name": module_id, "module_version": report[0]["version"],
                                                   "module_routed": True}
        assert routing.binding(kind, "node-2") == routing.BLOCKED  # not placed there
    workers = [{"node_name": "node-1"}, {"node_name": "node-2"}]
    assert routing.eligible(workers, kinds[0]) == [{"node_name": "node-1"}]
    lifecycle.deactivate(module_id)
    assert routing.eligible(workers, kinds[0]) == []
    with pytest.raises(Exception, match="release"):
        lifecycle.update({"bundled": module_id}, confirmed=True)
    lifecycle.uninstall(module_id, confirmed=True)
    assert routing.eligible(workers, kinds[0]) == workers


@pytest.mark.parametrize("role,kind", [("gpu", "image"), ("voice", "voice"), ("publisher", "publish"), ("backup", "backup")])
def test_worker_runs_the_same_executor_through_the_module_path_with_one_owner(monkeypatch, role, kind):
    monkeypatch.setattr(migration, "_modules", {})
    calls = []

    def legacy(role_name, task, _migration=False):
        calls.append((role_name, task["task"], _migration))
        return [{"filename": "out.json", "kind": "result", "data_base64": "e30="}]
    name = migration.ROUTES[role][0]
    monkeypatch.delenv(f"VERTEP_{name.upper()}_MODULE_ENABLED", raising=False)
    monkeypatch.delenv(f"VERTEP_{name.upper()}_MODULE_SHADOW", raising=False)
    plain = migration.execute(role, {"task": kind, "task_id": "t1", "lease_id": "l1"}, legacy)
    assert migration.overview()[name]["last_execution"]["path"] == "role"
    routed = migration.execute(role, {"task": kind, "task_id": "t2", "lease_id": "l2", "module_routed": True}, legacy)
    assert migration.overview()[name]["last_execution"]["path"] == "module"
    assert routed == plain and calls == [(role, kind, True), (role, kind, True)]  # one executor, one call each
    import threading
    gate, errors = threading.Event(), []

    def slow(role_name, task, _migration=False):
        gate.wait(5)
        return []
    first = threading.Thread(target=lambda: migration.execute(role, {"task": kind, "task_id": "t3", "lease_id": "l3"}, slow))
    first.start()
    import time
    time.sleep(0.2)
    try:
        # Same attempt re-delivered on the module path while the role path owns it.
        migration.execute(role, {"task": kind, "task_id": "t3", "lease_id": "l3", "module_routed": True}, slow)
    except RuntimeError as error:
        errors.append(str(error))
    gate.set()
    first.join()
    assert errors == ["Task already has an execution owner"]


def test_shadow_parity_replays_external_effects_once(monkeypatch):
    from worker.migration_trace import external
    effects = []

    def legacy(role_name, task, _migration=False):
        value = external(("publish", task["task_id"]), lambda: effects.append(task["task_id"]) or {"receipt": "r1"})
        return [{"filename": "receipt.json", "kind": "publication_receipt", "data_base64": json.dumps(value)}]
    monkeypatch.setattr(migration, "_modules", {})  # the module binds this executor, not one from another test
    monkeypatch.setenv("VERTEP_PUBLISHER_MODULE_SHADOW", "true")
    result = migration.execute("publisher", {"task": "publish", "task_id": "p1", "lease_id": "l1"}, legacy)
    record = migration.overview()["publisher"]["last_execution"]
    assert record["path"] == "shadow" and record["parity"] is True
    assert effects == ["p1"]  # shadow replays the recorded effect: no second publication
    assert result[0]["kind"] == "publication_receipt"


def test_bundled_manifests_are_the_release_catalog():
    assert {route[0] for route in migration.ROUTES.values()} <= set(BUNDLED)
    for name in BUNDLED:
        assert bundled_manifest(name)["name"] == name
