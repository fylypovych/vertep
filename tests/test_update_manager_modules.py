"""#512: Update Manager — approved plans, dependency order, parallel independent modules,
sequential nodes with health checks before READY, deferred nodes, CORE + module plans
and recovery to a compatible set."""

import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.module.lifecycle import ModuleLifecycle
from core.module.packages import verify_package
from core.module.trust import Publisher, TrustStore
from core.module.update_manager import PlanError, RolloutGates, UpdateManager
from tests.test_module_trust_isolation import CONTAINER, pem, sign

OK = {"container": True, "api": True, "database": True, "dependencies": True}


def package(key, name, version, dependencies=(), core_min="0.0.4.0"):
    manifest = {"schema_version": "2.0", "name": name, "version": version, "capabilities": [], "kind": "application",
                "publisher": "acme", "provenance": {"repository": "github.com/acme/mods"}, "container": CONTAINER,
                "core_compatibility": {"min": core_min, "max": "0.0.9.99"},
                "dependencies": [{"name": d} for d in dependencies], "migrations": []}
    return sign(key, manifest, {})


@pytest.fixture
def world(tmp_path):
    key = Ed25519PrivateKey.generate()
    trust = TrustStore({"acme": Publisher("acme", {"acme-k1": pem(key)}, ["github.com/acme/mods"])})
    core = {"version": "0.0.4.20"}
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda p: verify_package(p, trust), drain_timeout=0.1,
                                system_versions={"core_runtime": core["version"]})
    packages = {}

    def publish(name, version, **kw):
        packages[(name, version)] = package(key, name, version, **kw)
        return packages[(name, version)]

    for name, deps in (("b", ()), ("c", ()), ("a", ("b",))):
        lifecycle.install(publish(name, "0.0.0.1", dependencies=deps))
        lifecycle.activate(name)
    state = {"online": {"n1", "n2"}, "health": {}, "calls": [], "maintenance": [], "core_requests": []}
    lock = threading.Lock()

    def health(module_id, node, version):
        with lock:
            state["calls"].append((module_id, node, version))
        return state["health"].get((module_id, node), OK)

    manager = UpdateManager(tmp_path / "lc", lifecycle,
                            placement=lambda module_id: ["n1", "n2"],
                            package_for=lambda m, v: packages[(m, v)],
                            core_version=lambda: core["version"],
                            request_core_update=lambda v: state["core_requests"].append(v),
                            health=lambda m, n, v: health(m, n, v),
                            node_online=lambda n: n in state["online"],
                            maintenance=lambda m, on: state["maintenance"].append((m, on)),
                            wait_seconds=0.5, poll=0.01)
    return {"lifecycle": lifecycle, "manager": manager, "publish": publish, "state": state, "core": core}


def test_a_plan_names_versions_stages_and_nodes_and_runs_only_after_approval(world):
    for name, deps in (("a", ("b",)), ("b", ()), ("c", ())):
        world["publish"](name, "0.0.0.2", dependencies=deps)
    plan = world["manager"].plan({"a": "0.0.0.2", "b": "0.0.0.2", "c": "0.0.0.2"})
    assert plan["status"] == "PENDING_APPROVAL" and plan["stages"] == [["b", "c"], ["a"]]
    step = next(s for s in plan["steps"] if s["component"] == "a")
    assert step["from"] == "0.0.0.1" and step["to"] == "0.0.0.2" and step["depends_on"] == ["b"]
    assert [n["node"] for n in step["nodes"]] == ["n1", "n2"]
    with pytest.raises(PlanError, match="approved"):
        world["manager"].execute(plan["id"])
    world["manager"].approve(plan["id"], "admin")
    done = world["manager"].execute(plan["id"])
    assert done["status"] == "DONE"
    assert {m: world["lifecycle"].record(m)["version"] for m in "abc"} == {"a": "0.0.0.2", "b": "0.0.0.2", "c": "0.0.0.2"}
    ready = [e["module"] for e in done["events"] if e["message"] == "module ready"]
    assert ready.index("a") > max(ready.index("b"), ready.index("c"))  # dependency order
    # Maintenance: each module's new tasks stay queued while it updates, then dispatch resumes.
    assert sorted(set(world["state"]["maintenance"])) == sorted({(m, on) for m in "abc" for on in (True, False)})


def test_independent_modules_update_in_parallel_and_nodes_one_by_one(world):
    for name in ("b", "c"):
        world["publish"](name, "0.0.0.2")
    barrier = threading.Barrier(2, timeout=5)
    seen = []
    original = world["manager"].health

    def health(module_id, node, version):
        if node == "n1":
            barrier.wait()  # b and c are both in flight at the same time
        seen.append((module_id, node, world["manager"].gates.allowed(module_id, "n2")))
        return original(module_id, node, version)
    world["manager"].health = health
    plan = world["manager"].plan({"b": "0.0.0.2", "c": "0.0.0.2"})
    world["manager"].approve(plan["id"], "admin")
    assert world["manager"].execute(plan["id"])["status"] == "DONE"
    for module in ("b", "c"):
        order = [(node, n2_released) for m, node, n2_released in seen if m == module]
        assert order[0] == ("n1", False)  # n2 still on the previous package while n1 is checked
        assert order[-1][0] == "n2"


@pytest.mark.parametrize("failed_check", ["container", "api", "database", "dependencies"])
def test_every_check_must_pass_before_ready_and_a_failure_recovers(world, failed_check):
    world["publish"]("c", "0.0.0.2")
    world["publish"]("a", "0.0.0.2", dependencies=("c",))
    world["state"]["health"][("c", "n2")] = {**OK, failed_check: False}
    plan = world["manager"].plan({"a": "0.0.0.2", "c": "0.0.0.2"})
    world["manager"].approve(plan["id"], "admin")
    result = world["manager"].execute(plan["id"])
    assert result["status"] == "FAILED" and result["rolled_back"] == ["c"]
    assert result["rollback_set"] == ["a", "c"]  # a depends on the failed version: never started
    assert world["lifecycle"].record("c")["version"] == "0.0.0.1"
    assert world["lifecycle"].record("a")["version"] == "0.0.0.1"
    node = next(n for s in result["steps"] if s["component"] == "c" for n in s["nodes"] if n["node"] == "n2")
    assert node["status"] == "FAILED" and node["checks"][failed_check] is False


def test_an_offline_node_is_deferred_and_rechecked_after_reconnect(world):
    world["publish"]("b", "0.0.0.2")
    world["state"]["online"] = {"n1"}
    plan = world["manager"].plan({"b": "0.0.0.2"})
    world["manager"].approve(plan["id"], "admin")
    result = world["manager"].execute(plan["id"])
    assert result["status"] == "DEFERRED_NODES"
    assert not world["manager"].gates.allowed("b", "n2")
    world["state"]["online"] = {"n1", "n2"}
    assert world["manager"].resume_node("n2") == [{"plan": plan["id"], "module": "b", "ok": True}]
    assert world["manager"].get(plan["id"])["status"] == "DONE" and world["manager"].gates.allowed("b", "n2")


def test_a_reconnect_after_the_module_changed_marks_the_node_stale(world):
    world["publish"]("b", "0.0.0.2")
    world["state"]["online"] = {"n1"}
    plan = world["manager"].plan({"b": "0.0.0.2"})
    world["manager"].approve(plan["id"], "admin")
    world["manager"].execute(plan["id"])
    world["lifecycle"].rollback("b")  # the administrator went back meanwhile
    assert world["manager"].resume_node("n2") == []
    node = next(n for s in world["manager"].get(plan["id"])["steps"] for n in s.get("nodes", []) if n["node"] == "n2")
    assert node["status"] == "STALE"


def test_a_module_needing_a_newer_core_gets_a_joint_core_plus_module_plan(world):
    world["publish"]("c", "0.0.0.2", core_min="0.0.4.25")
    with pytest.raises(PlanError, match="newer CORE"):
        world["manager"].plan({"c": "0.0.0.2"})
    plan = world["manager"].plan({"c": "0.0.0.2"}, core_target="0.0.4.25")
    assert [s["component"] for s in plan["steps"]] == ["core", "c"]
    world["manager"].approve(plan["id"], "admin")
    waiting = world["manager"].execute(plan["id"])
    assert waiting["status"] == "WAITING_CORE" and world["state"]["core_requests"] == ["0.0.4.25"]
    assert world["lifecycle"].record("c")["version"] == "0.0.0.1"  # nothing before CORE
    # The independent Agent updated CORE (and this Manager); the plan continues.
    world["core"]["version"] = "0.0.4.25"
    world["lifecycle"].system_versions["core_runtime"] = "0.0.4.25"
    assert world["manager"].execute(plan["id"])["status"] == "DONE"
    assert world["lifecycle"].record("c")["version"] == "0.0.0.2"
    assert world["state"]["core_requests"] == ["0.0.4.25"]  # requested once


def test_a_core_target_incompatible_with_an_installed_module_is_refused(world):
    world["publish"]("c", "0.0.0.2", core_min="0.0.4.25")
    with pytest.raises(PlanError, match="Not compatible with CORE 0.0.10.0"):
        world["manager"].plan({"c": "0.0.0.2"}, core_target="0.0.10.0")


def test_rollout_gates_release_nodes_one_by_one(tmp_path):
    gates = RolloutGates(tmp_path / "gates.json")
    assert gates.allowed("b", "n1")  # no rollout in progress
    gates.open("b", "0.0.0.2")
    assert not gates.allowed("b", "n1") and not gates.allowed("b", "n2")
    gates.allow("b", "n1")
    assert gates.allowed("b", "n1") and not gates.allowed("b", "n2")
    gates.close("b")
    assert gates.allowed("b", "n2")
