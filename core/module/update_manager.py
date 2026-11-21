"""Update Manager (#512, VAR #509): approved plans, dependency order, sequential node rollout.

A plan names concrete versions, nodes and dependencies. Nothing runs before the
administrator approves it. When a module needs a newer CORE, the plan starts with
the CORE step, which the independent Update Agent performs (CORE and this Manager
are updated by it); the Manager continues the approved plan after its restart.

Modules are grouped into stages by their dependencies; independent modules of a
stage update in parallel. Inside one module the nodes are released one by one:
a node receives the new package only when the plan lets it (``rollout gate``);
the others keep the previous version. Before a node counts as READY its container,
API, database and required dependencies are checked. An offline node is deferred
and re-checked when it reconnects. During the maintenance of a module its new tasks
stay queued. A failure starts recovery: the failed module and every already updated
component that depends on its new version are rolled back to a compatible set.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .manifest import ModuleManifest

PENDING, APPROVED, RUNNING, WAITING_CORE, DONE, FAILED = (
    "PENDING_APPROVAL", "APPROVED", "RUNNING", "WAITING_CORE", "DONE", "FAILED")
HEALTH_CHECKS = ("container", "api", "database", "dependencies")


class PlanError(ValueError):
    pass


_log_lock = threading.Lock()


_apply_locks: dict[str, threading.Lock] = {}
_apply_locks_guard = threading.Lock()


def update_log(message: str, operation_id: str | None, **fields) -> None:
    """#515: every update event goes to the existing journal under category UPDATE, linked to
    its operation (plan id or lifecycle operation). Written straight to the structured log
    files /api/logs reads, so the journal does not depend on logger levels or handlers."""
    import os
    from datetime import datetime, timezone
    from core.logging_config import secret_redact
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": "INFO", "logger": "core",
              "message": f"UPDATE {message}", "category": "UPDATE", "operation_id": operation_id}
    for key, value in fields.items():
        name = "component" if key == "module" else key
        record[name] = value if isinstance(value, (dict, list, int, float, bool)) or value is None else secret_redact(str(value))
    root = Path(os.getenv("LOG_ROOT", "logs"))
    try:
        root.mkdir(parents=True, exist_ok=True)
        with _log_lock, open(root / "update.jsonl", "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # the plan file keeps the same events; the journal copy is best effort


def _dependency_names(manifest: ModuleManifest) -> list[str]:
    names = []
    for item in manifest.dependencies:
        name = (item.get("module") or item.get("name")) if isinstance(item, dict) else str(item)
        if name:
            names.append(name)
    return names


class RolloutGates:
    """Which nodes may already receive a module's new package (read by node assignments)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def open(self, module_id: str, version: str) -> None:
        with self._lock:
            data = self._read()
            data[module_id] = {"version": version, "nodes": []}
            self._write(data)

    def allow(self, module_id: str, node_id: str) -> None:
        with self._lock:
            data = self._read()
            entry = data.setdefault(module_id, {"version": None, "nodes": []})
            if node_id not in entry["nodes"]:
                entry["nodes"].append(node_id)
            self._write(data)

    def close(self, module_id: str) -> None:
        with self._lock:
            data = self._read()
            data.pop(module_id, None)
            self._write(data)

    def allowed(self, module_id: str, node_id: str) -> bool:
        """True when no rollout is in progress for the module or this node was released."""
        entry = self._read().get(module_id)
        return entry is None or node_id in entry.get("nodes", [])


class UpdateManager:
    def __init__(self, root: Path, lifecycle, *, placement: Callable[[str], list[str]],
                 package_for: Callable[[str, str], dict], core_version: Callable[[], str],
                 request_core_update: Callable[[str], None],
                 health: Callable[[str, str, str], dict[str, bool]],
                 node_online: Callable[[str], bool],
                 maintenance: Callable[[str, bool], None] = lambda module_id, on: None,
                 wait_seconds: float = 120.0, poll: float = 0.5) -> None:
        self.root = Path(root)
        self.lifecycle = lifecycle
        self.placement = placement
        self.package_for = package_for
        self.core_version = core_version
        self.request_core_update = request_core_update
        self.health = health
        self.node_online = node_online
        self.maintenance = maintenance
        self.wait_seconds = wait_seconds
        self.poll = poll
        self.gates = RolloutGates(self.root / "rollout-gates.json")
        self._lock = threading.RLock()
        # Shared by every manager of this installation: the API builds one per request, so an
        # instance lock would not keep a panel rollback apart from a running update (#515).
        with _apply_locks_guard:
            self._apply_lock = _apply_locks.setdefault(str(self.root.resolve()), threading.Lock())

    # -- persistence -------------------------------------------------------------------
    def _path(self, plan_id: str) -> Path:
        return self.root / "plans" / f"{plan_id}.json"

    def get(self, plan_id: str) -> dict:
        try:
            return json.loads(self._path(plan_id).read_text(encoding="utf-8"))
        except OSError as error:
            raise PlanError("Unknown update plan") from error

    def plans(self) -> list[dict]:
        folder = self.root / "plans"
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []

    def _save(self, plan: dict) -> None:
        path = self._path(plan["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _event(self, plan: dict, message: str, **fields) -> None:
        with self._lock:  # parallel modules of one stage share the plan
            plan.setdefault("events", []).append({"at": time.time(), "message": message, **fields})
            self._save(plan)
        update_log(message, plan["id"], **{"plan_status": plan.get("status"), **fields})

    # -- planning ------------------------------------------------------------------------
    def plan(self, targets: dict[str, str], core_target: str | None = None) -> dict:
        """Concrete versions, dependency stages and nodes; status PENDING_APPROVAL."""
        from packaging.version import Version
        if not targets and not core_target:
            raise PlanError("Nothing to update")
        manifests = {}
        for module_id, version in targets.items():
            manifest = self.lifecycle.verify(self.package_for(module_id, version))
            if manifest.module_id != module_id or manifest.version != version:
                raise PlanError(f"Package for {module_id} {version} is a different module or version")
            manifests[module_id] = manifest
        current_core = self.core_version()
        needs_core = [m for m, manifest in manifests.items() if not manifest.core_compatible(current_core)]
        steps = []
        if needs_core:
            if not core_target:
                raise PlanError(f"{', '.join(needs_core)} need a newer CORE; add the CORE version to the plan")
        if core_target:
            if Version(core_target) <= Version(current_core):
                raise PlanError("CORE target must be newer than the running CORE")
            incompatible = [m for m, manifest in manifests.items() if not manifest.core_compatible(core_target)]
            installed = {m: r for m, r in self.lifecycle.records().items() if m not in manifests}
            incompatible += [m for m, r in installed.items()
                             if not self.lifecycle._manifest(r).core_compatible(core_target)]
            if incompatible:
                raise PlanError(f"Not compatible with CORE {core_target}: {', '.join(sorted(incompatible))}")
            steps.append({"kind": "core", "component": "core", "from": current_core, "to": core_target,
                          "status": "PENDING"})
        # Dependency stages among the targets (installed dependencies are already present).
        remaining = dict(manifests)
        stages = []
        while remaining:
            ready = sorted(m for m, manifest in remaining.items()
                           if not set(_dependency_names(manifest)) & set(remaining) - {m})
            if not ready:
                raise PlanError("Dependency cycle between planned modules")
            stages.append(ready)
            for module_id in ready:
                remaining.pop(module_id)
        for index, stage in enumerate(stages):
            for module_id in stage:
                record = self.lifecycle.record(module_id)
                if record is None:
                    raise PlanError(f"{module_id} is not installed")
                steps.append({"kind": "module", "component": module_id, "stage": index,
                              "from": record["version"], "to": targets[module_id],
                              "depends_on": sorted(set(_dependency_names(manifests[module_id])) & set(targets)),
                              "nodes": [{"node": node, "status": "PENDING"} for node in sorted(self.placement(module_id))],
                              "status": "PENDING"})
        plan = {"id": uuid.uuid4().hex, "status": PENDING, "created_at": time.time(), "steps": steps,
                "stages": stages, "approved_by": None}
        self._save(plan)
        update_log("plan created", plan["id"], targets=targets, core_target=core_target)
        return plan

    def approve(self, plan_id: str, actor: str) -> dict:
        with self._lock:
            plan = self.get(plan_id)
            if plan["status"] != PENDING:
                raise PlanError(f"Plan is {plan['status']}, not waiting for approval")
            plan.update(status=APPROVED, approved_by=actor, approved_at=time.time())
            self._event(plan, "approved", actor=actor)
            return plan

    # -- execution ------------------------------------------------------------------------
    def execute(self, plan_id: str) -> dict:
        with self._lock:
            plan = self.get(plan_id)
            if plan["status"] not in {APPROVED, WAITING_CORE, RUNNING}:
                raise PlanError("Only an approved plan is executed")
            plan["status"] = RUNNING
            self._save(plan)
        core = next((s for s in plan["steps"] if s["kind"] == "core"), None)
        if core and core["status"] != "DONE":
            from packaging.version import Version
            if Version(self.core_version()) >= Version(core["to"]):
                core["status"] = "DONE"
                self._event(plan, "core updated", version=core["to"])
            else:
                if core["status"] == "PENDING":
                    self.request_core_update(core["to"])
                    core["status"] = "REQUESTED"
                plan["status"] = WAITING_CORE
                self._event(plan, "core update requested from the independent Update Agent", version=core["to"])
                return plan
        for stage in plan["stages"]:
            steps = [s for s in plan["steps"] if s["kind"] == "module" and s["component"] in stage
                     and s["status"] in {"PENDING", "RUNNING"}]
            with ThreadPoolExecutor(max_workers=max(1, len(steps))) as pool:
                results = list(pool.map(lambda step: self._module(plan, step), steps))
            if not all(results):
                return self._recover(plan)
        deferred = any(n["status"] == "DEFERRED" for s in plan["steps"] if s["kind"] == "module" for n in s["nodes"])
        plan["status"] = "DEFERRED_NODES" if deferred else DONE
        self._event(plan, "finished", status=plan["status"])
        return plan

    def _module(self, plan: dict, step: dict) -> bool:
        module_id, version = step["component"], step["to"]
        step["status"] = "RUNNING"
        self._event(plan, "module started", module=module_id, version=version)
        self.gates.open(module_id, version)
        self.maintenance(module_id, True)  # new tasks of this module stay queued
        try:
            record = self.lifecycle.record(module_id)
            online = [n for n in step["nodes"] if n["status"] != "DONE" and self.node_online(n["node"])]
            if record["version"] != version:
                if not online and step["nodes"]:
                    for node in step["nodes"]:
                        node["status"] = "DEFERRED"
                    step["status"] = "DEFERRED"
                    self._event(plan, "no node online, module deferred", module=module_id)
                    return True
                if online:
                    # The first node takes the new package before the update's own self-test.
                    self.gates.allow(module_id, online[0]["node"])
                with self._apply_lock:
                    # Applying packages is short and checks every active module's dependencies,
                    # so it is serialized; node rollouts and health checks stay parallel.
                    self.lifecycle.update(self.package_for(module_id, version), confirmed=True)
            for node in step["nodes"]:
                if node["status"] == "DONE":
                    continue
                if not self.node_online(node["node"]):
                    node["status"] = "DEFERRED"
                    self._event(plan, "node offline, deferred", module=module_id, node=node["node"])
                    continue
                if not self._release_node(plan, step, node):
                    step["status"] = "FAILED"
                    self._event(plan, "module failed", module=module_id, node=node["node"])
                    return False
            step["status"] = "DONE"
            self._event(plan, "module ready", module=module_id, version=version)
            return True
        except Exception as error:  # noqa: BLE001 - every failure leads to recovery
            step["status"] = "FAILED"
            self._event(plan, "module failed", module=module_id, error=str(error))
            return False
        finally:
            if step["status"] == "DONE" and not any(n["status"] == "DEFERRED" for n in step["nodes"]):
                self.gates.close(module_id)
            self.maintenance(module_id, False)

    def _release_node(self, plan: dict, step: dict, node: dict) -> bool:
        """Let one node take the new package, then require every health check before READY."""
        self.gates.allow(step["component"], node["node"])
        deadline = time.monotonic() + self.wait_seconds
        checks: dict[str, bool] = {}
        while time.monotonic() < deadline:
            checks = self.health(step["component"], node["node"], step["to"])
            if all(checks.get(name) for name in HEALTH_CHECKS):
                node.update(status="DONE", checks=checks)
                self._event(plan, "node ready", module=step["component"], node=node["node"], checks=checks)
                return True
            time.sleep(self.poll)
        node.update(status="FAILED", checks=checks)
        return False

    def _recover(self, plan: dict) -> dict:
        """Roll back the failed module and every updated module that depends on it."""
        failed = {s["component"] for s in plan["steps"] if s["kind"] == "module" and s["status"] == "FAILED"}
        dependents = set(failed)
        changed = True
        while changed:
            changed = False
            for step in plan["steps"]:
                if step["kind"] == "module" and step["component"] not in dependents and set(step.get("depends_on", [])) & dependents:
                    dependents.add(step["component"])
                    changed = True
        rolled_back = []
        for step in reversed(plan["steps"]):
            if step["kind"] != "module" or step["component"] not in dependents:
                continue
            record = self.lifecycle.record(step["component"])
            if record and record["version"] == step["to"] and record.get("previous"):
                self.lifecycle.rollback(step["component"])
                rolled_back.append(step["component"])
            self.gates.close(step["component"])
            step["status"] = "ROLLED_BACK" if step["component"] in rolled_back else step["status"]
        plan.update(status=FAILED, rollback_set=sorted(dependents), rolled_back=rolled_back)
        self._event(plan, "recovery finished", rolled_back=rolled_back)
        return plan

    # -- reconnect -------------------------------------------------------------------------
    def rollback_to_point(self, module_id: str, point: str, *, accept_data_loss: bool, actor: str | None = None) -> dict:
        """Manual rollback from the Updates panel (#515): an Update Manager operation of its own,
        serialized with updates and recorded in the UPDATE journal under its operation id."""
        operation = f"rollback-{uuid.uuid4().hex}"
        before = (self.lifecycle.record(module_id) or {}).get("version")
        update_log("rollback to point requested", operation, module=module_id, point=point,
                   from_version=before, accept_data_loss=accept_data_loss, actor=actor)
        try:
            with self._apply_lock:  # never in parallel with a running update of any module
                result = self.lifecycle.rollback_to_point(module_id, point, accept_data_loss=accept_data_loss)
        except Exception as error:
            update_log("rollback to point failed", operation, module=module_id, point=point, error=str(error))
            raise
        update_log("rollback to point completed", operation, module=module_id, point=point,
                   from_version=before, version=result.get("version"), state=result.get("state"))
        return {**result, "operation_id": operation}

    def resume_node(self, node_id: str) -> list[dict]:
        """A deferred node reconnected: re-check the plan still applies, then release it."""
        results = []
        for plan in self.plans():
            if plan["status"] not in {"DEFERRED_NODES", RUNNING}:
                continue
            for step in plan["steps"]:
                if step["kind"] != "module":
                    continue
                for node in step["nodes"]:
                    if node["node"] != node_id or node["status"] != "DEFERRED":
                        continue
                    record = self.lifecycle.record(step["component"])
                    if record and record["version"] == step["from"] and step["status"] == "DEFERRED":
                        # The module was deferred whole: the approved update still applies.
                        self.gates.allow(step["component"], node_id)
                        self.lifecycle.update(self.package_for(step["component"], step["to"]), confirmed=True)
                        record = self.lifecycle.record(step["component"])
                    if not record or record["version"] != step["to"]:
                        node["status"] = "STALE"
                        self._event(plan, "plan no longer applies to the node", module=step["component"], node=node_id)
                        continue
                    ok = self._release_node(plan, step, node)
                    if ok and step["status"] == "DEFERRED":
                        step["status"] = "DONE" if not any(n["status"] == "DEFERRED" for n in step["nodes"]) else "DEFERRED"
                    results.append({"plan": plan["id"], "module": step["component"], "ok": ok})
                    if ok and not any(n["status"] == "DEFERRED" for n in step["nodes"]):
                        self.gates.close(step["component"])
            if not any(n["status"] == "DEFERRED" for s in plan["steps"] if s["kind"] == "module" for n in s["nodes"]):
                if plan["status"] == "DEFERRED_NODES":
                    plan["status"] = DONE
                    self._event(plan, "all deferred nodes completed")
            else:
                self._save(plan)
        return results
