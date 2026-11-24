"""Panel navigation declared by modules, and the CORE pages that exist only with their module.

A module lists its menu entries in ``ui.navigation`` of its manifest. CORE shows an entry
only while the module has a READY instance: a deactivated, failed or removed module has no
menu entry, its built-in panel page is closed and its CORE API answers 503 (owner, 23.11.2026).
An entry points to the module's own signed UI (``/modules/<id>``), or, for an official module
whose page is built into the panel, to that page (``route``) when CORE lists it below.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

# Built-in panel pages owned by official modules. A manifest may point its menu entry only at
# a page listed for that module here, so a module cannot take over another page of the panel.
MODULE_PAGES: dict[str, tuple[str, ...]] = {
    "characters": ("/characters",),
    "brands": ("/brands",),
    "publishing": ("/published",),
}
MODULE_INACTIVE = "Module is not active"


def module_ready(module_id: str) -> bool:
    from .gateway import _modules_or_none, _ready_instance
    registry = _modules_or_none()
    return bool(registry is not None and _ready_instance(registry, module_id))


def require_module(module_id: str):
    """Route dependency: the CORE API of a module's page works only while the module is READY."""
    def check() -> None:
        if not module_ready(module_id):
            raise HTTPException(503, MODULE_INACTIVE)
    return Depends(check)


# A new job takes its character and brand from these modules (owner, 23.11.2026); jobs
# already running are not stopped by a deactivation (#499).
JOB_MODULES = {"characters": "Персонажі", "brands": "Бренди"}


def inactive_job_modules() -> list[str]:
    """Labels of the modules a new job needs that are not READY."""
    return [label for module_id, label in JOB_MODULES.items() if not module_ready(module_id)]


def navigation() -> dict:
    """Menu entries of READY modules; a page of the panel listed nowhere here stays closed."""
    from .gateway import _modules_or_none
    registry = _modules_or_none()
    items, seen = [], set()
    for instance in (registry.instances() if registry is not None else []):
        manifest = instance.manifest
        if instance.state != "READY" or not manifest.ui or manifest.module_id in seen:
            continue
        seen.add(manifest.module_id)  # several READY instances give one menu entry
        for entry in manifest.ui["navigation"]:
            route = entry.get("route")
            if route not in MODULE_PAGES.get(manifest.module_id, ()):
                if not manifest.ui["files"]:
                    continue  # neither its own UI nor a page of the panel
                route = f"/modules/{manifest.module_id}"
            items.append({"module_id": manifest.module_id, "id": entry["id"],
                          "label": entry.get("label") or manifest.module_id, "path": route,
                          "group": entry.get("group") or "modules", "admin_only": bool(entry.get("admin_only"))})
    return {"items": sorted(items, key=lambda item: (item["module_id"], item["id"]))}
