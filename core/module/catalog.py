"""Administrator module catalog: modules x nodes with honest compatibility (#496).

One user-facing choice — module and target node — over the internal role,
Worker and capability mechanics. A module that a node cannot run (no GPU, too
little memory, wrong node profile for LAB, no runtime for the bundled
function) is reported with the reason and its placement is refused; nothing
is shown as READY without a Worker report and the module lifecycle.
"""

from __future__ import annotations

from .lifecycle import BUNDLED, bundled_manifest
from .manifest import ModuleManifest

BUNDLED_LABELS = {"text": "Текст і сценарії", "image": "Зображення та відео", "audio": "Голос",
                  "publisher": "Публікація", "backup": "Резервні копії", "characters": "Персонажі"}


def _deployed_roles(node: dict) -> set[str]:
    """Runtime roles a node actually runs (the legacy role catalog is the deployment detail)."""
    role = node.get("role") or ""
    if role != "core":
        return {role}
    from core.first_run import config_root
    import json
    try:
        plan = json.loads((config_root() / "deployment-plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan = {}
    return {"core", *plan.get("additional_roles", [])}


def nodes() -> list[dict]:
    from core.node_registry import registered_nodes
    from core.state import store
    result = {}
    for node in registered_nodes():
        if node.get("revoked_at"):
            continue
        result[node["node_id"]] = {"node_id": node["node_id"], "role": node.get("role")}
    for name, worker in store.workers.items():
        result.setdefault(name, {"node_id": name, "role": worker.get("role")})
        result[name].update(role=result[name]["role"] or worker.get("role"), status=worker.get("status"),
                            gpu_available=bool(worker.get("gpu_available")) or int(worker.get("vram_mb") or 0) > 0,
                            vram_mb=int(worker.get("vram_mb") or 0), cpu_cores=int(worker.get("cpu_cores") or 0),
                            ram_mb=int(worker.get("ram_mb") or 0))
    return sorted(result.values(), key=lambda item: item["node_id"])


def compatibility(manifest: ModuleManifest, node: dict, profile: dict, bundled_role: str | None = None) -> list[str]:
    """Human reasons why this node cannot run the module; empty when it can."""
    reasons = []
    requirements = manifest.resource_requirements
    if manifest.placement["requires_gpu"] and not node.get("gpu_available"):
        reasons.append(f"Модуль потребує GPU ({requirements.get('min_vram_mb', 0)} МБ VRAM); на вузлі GPU немає")
    elif requirements.get("min_vram_mb") and node.get("vram_mb") and node["vram_mb"] < requirements["min_vram_mb"]:
        reasons.append(f"Недостатньо VRAM: потрібно {requirements['min_vram_mb']} МБ, є {node['vram_mb']} МБ")
    if requirements.get("min_ram_mb") and node.get("ram_mb") and node["ram_mb"] < requirements["min_ram_mb"]:
        reasons.append(f"Недостатньо RAM: потрібно {requirements['min_ram_mb']} МБ, є {node['ram_mb']} МБ")
    if requirements.get("min_cpu_cores") and node.get("cpu_cores") and node["cpu_cores"] < requirements["min_cpu_cores"]:
        reasons.append(f"Недостатньо CPU: потрібно {requirements['min_cpu_cores']}, є {node['cpu_cores']}")
    if manifest.placement["target"] == "isolated_lab" and profile != {"environment": "nonproduction", "isolation": "vm"}:
        reasons.append("LAB розміщується лише на окремому nonproduction вузлі у власній VM")
    if bundled_role and bundled_role not in _deployed_roles(node):
        reasons.append("На вузлі не розгорнуто середовище виконання цієї функції")
    return reasons


def bundled_role(module_id: str) -> str | None:
    return bundled_manifest(module_id).get("role") if module_id in BUNDLED else None


def check_placement(registry, module_id: str, node_ids: list[str]) -> None:
    """Refuse placements a node cannot honour, with the reason (raises ValueError)."""
    known = {node["node_id"]: node for node in nodes()}
    manifest = registry.installed_manifest(module_id)
    if manifest is None and module_id in BUNDLED:
        manifest = ModuleManifest.from_dict(bundled_manifest(module_id))
    if manifest is None:
        return  # registry.set_placement reports unknown modules
    for node_id in node_ids:
        if node_id not in known:
            raise ValueError(f"Вузол {node_id} не зареєстрований")
        reasons = compatibility(manifest, known[node_id], registry.placement.node_profile(node_id), bundled_role(module_id))
        if reasons:
            raise ValueError(f"Вузол {node_id}: " + "; ".join(reasons))


def catalog(runtime) -> dict:
    registry, lifecycle = runtime.modules, runtime.lifecycle
    records = lifecycle.records()
    entries = {}
    for module_id in BUNDLED:
        entries[module_id] = (ModuleManifest.from_dict(bundled_manifest(module_id)), True)
    for module_id, record in records.items():
        if module_id not in entries:
            entries[module_id] = (ModuleManifest.from_dict(record["package"]["manifest"]), False)
    known = nodes()
    result = []
    for module_id, (manifest, bundled) in sorted(entries.items()):
        record = records.get(module_id)
        instances = {i.node_id: i.to_dict() for i in registry.instances(module_id)}
        result.append({
            "module_id": module_id, "label": BUNDLED_LABELS.get(module_id) or (manifest.ui or {}).get("navigation", [{}])[0].get("label") or module_id,
            "version": manifest.version, "kind": manifest.kind, "bundled": bundled,
            "runtime_role": bundled_role(module_id) if bundled else None,
            "state": record["state"] if record else "NOT_INSTALLED",
            "requires_gpu": manifest.placement["requires_gpu"], "target": manifest.placement["target"],
            "scaling": manifest.placement["scaling"], "resources": manifest.resource_requirements,
            "dependencies": manifest.dependencies, "permissions": manifest.permissions,
            "approved_permissions": registry.placement.approvals.get(module_id, []),
            "placement": registry.placement.placement.get(module_id, []),
            "nodes": [{"node_id": node["node_id"],
                       "compatible_reasons": compatibility(manifest, node, registry.placement.node_profile(node["node_id"]),
                                                           bundled_role(module_id) if bundled else None),
                       # Only the runtime of the function is missing (the node could deploy it).
                       "runtime_missing": bool(bundled) and bundled_role(module_id) not in _deployed_roles(node)
                       and not compatibility(manifest, node, registry.placement.node_profile(node["node_id"])),
                       "instance": instances.get(node["node_id"])} for node in known],
        })
    return {"modules": result, "nodes": [{**node, **registry.placement.node_profile(node["node_id"])} for node in known]}
