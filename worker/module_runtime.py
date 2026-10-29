"""Load only verified signed module code and retain active call snapshots."""

import base64
import os
from pathlib import Path

from core.module.packages import ModulePackages


def packages():
    project = Path(__file__).resolve().parents[1]
    root = os.getenv("VERTEP_MODULE_ROOT") or str(project / "data/modules")
    if not os.getenv("VERTEP_MODULE_ROOT") and not Path(root).exists():
        return None
    trust = Path(os.getenv("VERTEP_MODULE_TRUST_ROOT", str(project / "config/module-trust")))
    return ModulePackages(Path(root), {path.stem: path.read_bytes() for path in trust.glob("*.pem")})


def load_module(manager, name):
    manager.info(name)  # Must still be enabled with compatible dependencies.
    package = manager._records[name]["package"]
    manager.verify(package)
    source = base64.b64decode(package["files"]["module.py"], validate=True)
    namespace = {"__name__": f"vertep_module_{name}"}
    exec(compile(source, f"signed-module:{name}", "exec"), namespace)
    for method in ("execute", "cancel", "health", "resource_query"):
        if not callable(namespace.get(method)):
            raise ValueError(f"Module is missing {method} API")
    return namespace


def inventory():
    manager = packages()
    if manager is None:
        return []
    result = []
    for manifest in manager.registry.list_modules():
        try:
            module = load_module(manager, manifest.name)
            health = module["health"]()
            resources = module["resource_query"]()
            if health.get("ready") is True and isinstance(resources, dict):
                result.append({**manifest.to_dict(), "ready": True, "resources": resources})
        except Exception:
            # Installation alone never attests that a module is executable.
            continue
    return result


def execute_module(name, task):
    manager = packages()
    if manager is None:
        raise RuntimeError("Module storage is not configured")
    if task.get("module_version") and manager.info(name)["version"] != task["module_version"]:
        raise ValueError("Installed module version differs from assigned version")
    module = load_module(manager, name)
    if module["health"]().get("ready") is not True:
        raise RuntimeError("Module is not ready")
    # Keep this verified namespace alive until its call ends. A concurrent CLI
    # uninstall/replacement affects subsequent calls, not this active snapshot.
    return module["execute"](task)
