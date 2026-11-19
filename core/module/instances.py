"""Node / module definition / node-module instance registry.

Three identities are kept apart:

* **node** - an enrolled server or VM (``node_id``) with an admin-declared
  profile (``environment``, ``isolation``);
* **module definition** - ``module_id`` + ``version`` from a manifest;
* **instance** - one definition installed on one node (``node_id``,
  ``module_id``). Several nodes may hold instances of the same module; a
  report from one node never replaces another node's instance.

Placement (where a module may run) is chosen by the administrator and is
persisted separately. CORE dispatches only to READY instances that are
placed, compatible and whose dependencies resolve. Stateful application
modules are ``single_owner``: exactly one placed node, and placement is not
moved while the current owner instance is READY.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Iterable

from .manifest import ModuleManifest

READY = "READY"
NOT_READY = "NOT_READY"
NODE_ENVIRONMENTS = ("production", "nonproduction")
NODE_ISOLATION = ("host", "vm")
SYSTEM_DEPENDENCIES = {"core_runtime", "python_runtime", "docker_runtime", "gpu_runtime"}


def default_placement_path() -> Path:
    configured = os.getenv("VERTEP_MODULE_PLACEMENT_FILE")
    if configured:
        return Path(configured)
    from core.first_run import config_root
    return config_root() / "module-placement.json"


class PlacementStore:
    """Admin-owned node profiles and module placement, persisted atomically."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = RLock()
        self.nodes: dict[str, dict] = {}
        self.placement: dict[str, list[str]] = {}
        self.approvals: dict[str, list[str]] = {}
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.nodes = {str(k): dict(v) for k, v in data.get("nodes", {}).items()}
            self.placement = {str(k): list(v) for k, v in data.get("placement", {}).items()}
            self.approvals = {str(k): list(v) for k, v in data.get("approvals", {}).items()}

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"nodes": self.nodes, "placement": self.placement, "approvals": self.approvals},
                                        ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def node_profile(self, node_id: str) -> dict:
        profile = self.nodes.get(node_id, {})
        return {"environment": profile.get("environment", "production"), "isolation": profile.get("isolation", "host")}

    def set_node_profile(self, node_id: str, environment: str, isolation: str) -> dict:
        if environment not in NODE_ENVIRONMENTS:
            raise ValueError("Node environment must be production or nonproduction")
        if isolation not in NODE_ISOLATION:
            raise ValueError("Node isolation must be host or vm")
        with self._lock:
            previous = dict(self.nodes)
            self.nodes[node_id] = {"environment": environment, "isolation": isolation}
            try:
                self._save()
            except OSError:
                self.nodes = previous
                raise
            return self.node_profile(node_id)

    def set_approval(self, module_id: str, permissions: list[str]) -> None:
        """Record the exact permission set the administrator approved."""
        with self._lock:
            previous = dict(self.approvals)
            if permissions:
                self.approvals[module_id] = sorted(set(permissions))
            else:
                self.approvals.pop(module_id, None)
            try:
                self._save()
            except OSError:
                self.approvals = previous
                raise

    def set_placement(self, module_id: str, node_ids: list[str]) -> None:
        with self._lock:
            previous = dict(self.placement)
            if node_ids:
                self.placement[module_id] = list(dict.fromkeys(node_ids))
            else:
                self.placement.pop(module_id, None)
            try:
                self._save()
            except OSError:
                self.placement = previous
                raise


@dataclass
class ModuleInstance:
    node_id: str
    manifest: ModuleManifest
    reported_ready: bool
    origin: str = "package"
    endpoint: str | None = None
    unflagged: bool = False  # release-bundled role module without its node env flag
    state: str = NOT_READY
    reasons: list[str] = field(default_factory=list)

    @property
    def module_id(self) -> str:
        return self.manifest.module_id

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "module_id": self.module_id, "name": self.manifest.name,
                "permissions": list(self.manifest.permissions), "endpoint": self.endpoint,
                "version": self.manifest.version, "kind": self.manifest.kind, "origin": self.origin,
                "state": self.state, "reasons": list(self.reasons)}


class ModuleInstanceRegistry:
    """CORE registry of nodes, module definitions and node/module instances."""

    def __init__(self, system_versions: dict | None = None, placement: PlacementStore | None = None) -> None:
        self.system_versions = dict(system_versions or {})
        self.placement = placement or PlacementStore()
        self._lock = RLock()
        self._instances: dict[tuple[str, str], ModuleInstance] = {}
        # module_id -> lifecycle state from core/module/lifecycle.py; modules
        # without a lifecycle record (legacy/bundled) are unaffected.
        self._lifecycle: dict[str, str] = {}
        self._installed: dict[str, ModuleManifest] = {}  # manifests of lifecycle-installed modules

    # -- reporting -------------------------------------------------------------
    def report_node(self, node_id: str, inventory: Iterable[dict]) -> list[ModuleInstance]:
        """Atomically replace *this node's* instances from a Worker inventory.

        Invalid manifests raise before any state changes; a duplicate module on
        one node is rejected. Other nodes' instances are untouched.
        """
        parsed: dict[str, ModuleInstance] = {}
        for item in inventory:
            manifest = ModuleManifest.from_dict(item)
            if manifest.module_id in parsed:
                raise ValueError(f"Duplicate module instance on node: {manifest.module_id}")
            endpoint = item.get("endpoint")
            if endpoint is not None and (not isinstance(endpoint, str) or not re.fullmatch(r"https?://[A-Za-z0-9.:\[\]-]+(?::\d+)?", endpoint)):
                raise ValueError("Invalid module instance endpoint")
            parsed[manifest.module_id] = ModuleInstance(node_id, manifest, item.get("ready") is True,
                                                        str(item.get("origin") or "package"), endpoint,
                                                        unflagged=item.get("origin") == "bundled" and item.get("enabled") is False)
        with self._lock:
            candidate = {key: value for key, value in self._instances.items() if key[0] != node_id}
            candidate.update({(node_id, module_id): instance for module_id, instance in parsed.items()})
            self._evaluate(candidate)
            self._instances = candidate
            return [candidate[(node_id, module_id)] for module_id in parsed]

    def remove_node(self, node_id: str) -> None:
        with self._lock:
            candidate = {key: value for key, value in self._instances.items() if key[0] != node_id}
            self._evaluate(candidate)
            self._instances = candidate

    # -- placement (administrator) ----------------------------------------------
    def set_node_profile(self, node_id: str, environment: str, isolation: str) -> dict:
        with self._lock:
            profile = self.placement.set_node_profile(node_id, environment, isolation)
            self._evaluate(self._instances)
            return profile

    def set_placement(self, module_id: str, node_ids: list[str]) -> dict:
        """Admin placement. Validates against known definitions and node profiles."""
        if not isinstance(node_ids, list) or any(not isinstance(n, str) or not n for n in node_ids):
            raise ValueError("Placement must be a list of node ids")
        with self._lock:
            definitions = [i.manifest for i in self._instances.values() if i.module_id == module_id]
            if module_id in self._installed:
                # Placement precedes the first Worker report for installed modules.
                definitions.append(self._installed[module_id])
            if not definitions:
                raise ValueError("Unknown module: no node reports this module")
            for manifest in definitions:
                self._check_placement(manifest, node_ids)
            current = self.placement.placement.get(module_id, [])
            if any(m.placement["scaling"] == "single_owner" for m in definitions):
                owners = [i for i in self._instances.values()
                          if i.module_id == module_id and i.state == READY and i.node_id not in node_ids]
                if owners and current:
                    # A live stateful service is never moved implicitly; it must
                    # first be deactivated through the module lifecycle.
                    raise ValueError("Single-owner module is READY on its current node; deactivate before moving")
            self.placement.set_placement(module_id, node_ids)
            self._evaluate(self._instances)
            return {"module_id": module_id, "node_ids": list(self.placement.placement.get(module_id, []))}

    def _check_placement(self, manifest: ModuleManifest, node_ids: list[str]) -> None:
        if manifest.placement["scaling"] == "single_owner" and len(node_ids) > 1:
            raise ValueError("Single-owner module may be placed on exactly one node")
        if manifest.placement["target"] == "isolated_lab":
            for node_id in node_ids:
                profile = self.placement.node_profile(node_id)
                if profile != {"environment": "nonproduction", "isolation": "vm"}:
                    raise ValueError("LAB placement requires a separate nonproduction VM node")

    def set_lifecycle_state(self, module_id: str, state: str | None, manifest: ModuleManifest | None = None) -> None:
        with self._lock:
            if state is None:
                self._lifecycle.pop(module_id, None)
                self._installed.pop(module_id, None)
            elif state == "UNINSTALLED":
                # Tombstone: a node still reporting the removed module gets no access.
                self._lifecycle[module_id] = state
                self._installed.pop(module_id, None)
            else:
                self._lifecycle[module_id] = state
                if manifest is not None:
                    self._installed[module_id] = manifest
            self._evaluate(self._instances)

    def installed_manifest(self, module_id: str) -> ModuleManifest | None:
        with self._lock:
            if module_id in self._installed:
                return self._installed[module_id]
            for instance in self._instances.values():
                if instance.module_id == module_id:
                    return instance.manifest
            return None

    def declared_permissions(self, module_id: str) -> set[str]:
        with self._lock:
            declared = {p for i in self._instances.values() if i.module_id == module_id for p in i.manifest.permissions}
            if module_id in self._installed:
                declared |= set(self._installed[module_id].permissions)
            return declared

    def approve_permissions(self, module_id: str, permissions: list[str]) -> dict:
        if not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions):
            raise ValueError("Permissions must be a list of strings")
        with self._lock:
            declared = {p for i in self._instances.values() if i.module_id == module_id for p in i.manifest.permissions}
            if module_id in self._installed:
                declared |= set(self._installed[module_id].permissions)
            if not declared and not permissions:
                pass
            elif set(permissions) - declared:
                raise ValueError("Approval may include only permissions the module declares")
            self.placement.set_approval(module_id, permissions)
            self._evaluate(self._instances)
            return {"module_id": module_id, "permissions": sorted(set(permissions))}

    # -- readiness -------------------------------------------------------------
    def _evaluate(self, instances: dict[tuple[str, str], ModuleInstance]) -> None:
        core_version = self.system_versions.get("core_runtime")
        placement = self.placement.placement
        for instance in instances.values():
            manifest = instance.manifest
            reasons = []
            if not instance.reported_ready:
                reasons.append("self_test_not_passed")
            if instance.unflagged and manifest.module_id not in self._lifecycle:
                # Neither flagged on the node nor managed by the module lifecycle:
                # the legacy role path only, no module capability.
                reasons.append("bundled_not_managed")
            lifecycle = self._lifecycle.get(manifest.module_id)
            if lifecycle is not None and lifecycle != "ACTIVE":
                # Deactivated, updating, failed or uninstalled: no new dispatch/API/UI.
                reasons.append("lifecycle_" + lifecycle.lower())
            if not manifest.core_compatible(core_version):
                reasons.append("core_incompatible")
            if not set(manifest.permissions) <= set(self.placement.approvals.get(manifest.module_id, [])):
                # A new or expanded permission set waits for administrator approval.
                reasons.append("permission_approval_required")
            if manifest.kind == "application" and instance.reported_ready and not instance.endpoint:
                reasons.append("endpoint_missing")
            placed = placement.get(manifest.module_id)
            if placed is None:
                # Legacy compatibility: replicated execution modules reported by a
                # Worker remain usable until the administrator sets placement.
                # Application and LAB modules always need explicit placement.
                if manifest.kind == "application" or manifest.placement["target"] == "isolated_lab":
                    reasons.append("placement_required")
            elif instance.node_id not in placed:
                reasons.append("not_placed_on_node")
            else:
                try:
                    self._check_placement(manifest, placed)
                except ValueError:
                    reasons.append("placement_invalid")
            instance.reasons = reasons
        # Distinct modules claiming one API namespace or provided interface are
        # both withheld (fail closed) instead of picking a winner by arrival order.
        claims: dict[tuple, set[str]] = {}
        for instance in instances.values():
            manifest = instance.manifest
            keys = [("api", manifest.api["namespace"])] if manifest.api else []
            keys += [("provides", p["type"], p["name"]) for p in manifest.provides]
            for key in keys:
                claims.setdefault(key, set()).add(manifest.module_id)
        contested = {module for owners in claims.values() if len(owners) > 1 for module in owners}
        for instance in instances.values():
            if instance.module_id in contested:
                instance.reasons.append("interface_conflict")
            instance.state = NOT_READY if instance.reasons else READY
        self._resolve_dependencies(instances)

    def _resolve_dependencies(self, instances: dict[tuple[str, str], ModuleInstance]) -> None:
        """Iteratively demote READY instances whose dependencies are unmet or cyclic."""
        from packaging.version import Version

        def dep_name(dep):
            return dep["name"] if isinstance(dep, dict) else dep

        graph: dict[str, set[str]] = {}
        for instance in instances.values():
            graph.setdefault(instance.module_id, set()).update(dep_name(d) for d in instance.manifest.dependencies)
        cyclic = set()
        for start in graph:
            stack, seen = [start], set()
            while stack:
                node = stack.pop()
                for nxt in graph.get(node, ()):
                    if nxt == start:
                        cyclic.add(start)
                    elif nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)

        def interface_ok(requirement, ready):
            for other in ready:
                for provided in other.manifest.provides:
                    if provided["type"] != requirement["type"] or provided["name"] != requirement["name"]:
                        continue
                    have = tuple(int(x) for x in provided["version"].split("."))
                    low = tuple(int(x) for x in requirement["min_version"].split("."))
                    if have < low:
                        continue
                    if "max_version" in requirement and have > tuple(int(x) for x in requirement["max_version"].split(".")):
                        continue
                    return True
            return False

        changed = True
        while changed:
            changed = False
            ready = [i for i in instances.values() if i.state == READY]
            for instance in ready:
                reasons = []
                if instance.module_id in cyclic:
                    reasons.append("dependency_cycle")
                for dep in instance.manifest.dependencies:
                    dependency = dep if isinstance(dep, dict) else {"name": dep}
                    name = dependency["name"]
                    if name in SYSTEM_DEPENDENCIES:
                        versions = [self.system_versions.get(name)] if name in self.system_versions else []
                        if not versions and (dependency.get("min_version") or dependency.get("max_version")):
                            reasons.append(f"dependency_unmet:{name}")
                            continue
                        if not versions:
                            continue
                    else:
                        versions = [i.manifest.version for i in ready if i.module_id == name]
                    if not versions or not any(
                            (not dependency.get("min_version") or Version(v) >= Version(dependency["min_version"])) and
                            (not dependency.get("max_version") or Version(v) <= Version(dependency["max_version"]))
                            for v in versions):
                        reasons.append(f"dependency_unmet:{name}")
                for requirement in instance.manifest.requires:
                    if requirement["type"] == "capability":
                        continue  # served by Worker capabilities through CORE, not by a module
                    if not interface_ok(requirement, [i for i in ready if i is not instance]):
                        reasons.append(f"interface_unmet:{requirement['type']}:{requirement['name']}")
                if reasons:
                    instance.reasons.extend(reasons)
                    instance.state = NOT_READY
                    changed = True

    # -- conflicts ---------------------------------------------------------------
    def conflicts(self) -> list[str]:
        """API namespace / route / interface conflicts between distinct modules."""
        owners: dict[tuple, str] = {}
        found = []
        for instance in self._instances.values():
            manifest = instance.manifest
            keys = []
            if manifest.api:
                keys.append(("api", manifest.api["namespace"]))
            keys += [("provides", p["type"], p["name"]) for p in manifest.provides]
            for key in keys:
                owner = owners.setdefault(key, manifest.module_id)
                if owner != manifest.module_id:
                    found.append(f"{':'.join(key)} claimed by {owner} and {manifest.module_id}")
        return sorted(set(found))

    # -- queries -----------------------------------------------------------------
    def instances(self, module_id: str | None = None) -> list[ModuleInstance]:
        with self._lock:
            return [i for i in self._instances.values() if module_id is None or i.module_id == module_id]

    def ready_instances(self, module_id: str) -> list[ModuleInstance]:
        return [i for i in self.instances(module_id) if i.state == READY]

    def get_module(self, module_id: str) -> ModuleManifest | None:
        """Highest READY definition (compatibility with ModuleRegistry.get_module)."""
        from packaging.version import Version
        ready = self.ready_instances(module_id)
        if not ready:
            return None
        return max((i.manifest for i in ready), key=lambda m: Version(m.version))

    def dispatch_targets(self, module_id: str) -> dict[str, dict]:
        """node_id -> assigned version/requirements for READY placed instances."""
        return {i.node_id: {"version": i.manifest.version,
                            "resource_requirements": dict(i.manifest.resource_requirements)}
                for i in self.ready_instances(module_id)}

    def node_capabilities(self, node_id: str) -> set[str]:
        return {cap for i in self.instances() if i.node_id == node_id and i.state == READY
                for cap in i.manifest.capabilities}

    def snapshot(self) -> dict:
        with self._lock:
            nodes = sorted({i.node_id for i in self._instances.values()} | set(self.placement.nodes))
            definitions = {}
            for instance in self._instances.values():
                definitions[f"{instance.module_id}@{instance.manifest.version}"] = instance.manifest.to_dict()
            return {"nodes": {n: self.placement.node_profile(n) for n in nodes},
                    "definitions": definitions,
                    "instances": [i.to_dict() for i in sorted(self._instances.values(), key=lambda x: (x.node_id, x.module_id))],
                    "placement": dict(self.placement.placement),
                    "approvals": dict(self.placement.approvals),
                    "conflicts": self.conflicts()}
