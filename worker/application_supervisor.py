"""Worker-side supervisor for Application Module containers on this node.

Each heartbeat the Worker asks CORE which application modules are placed and
approved for this node, verifies every signed package against the node's own
trust root, keeps exactly those containers running (restarting on version
change), stops containers that are no longer assigned and reports one
inventory item per module: ready only when its ``/health`` answers.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import logging

import httpx

from core.module.packages import verify_package
from worker import application_container as containers

logger = logging.getLogger(__name__)


def _verified(item: dict, trust):
    """A signed package, or a module shipped inside this node's own signed release: then
    the manifest (and pinned image) is this release's, never the copy CORE sent."""
    package = item["package"]
    if "bundled" in package:
        from core.module.lifecycle import bundled_manifest
        from core.module.manifest import ModuleManifest
        if package["bundled"] != item["module_id"]:
            raise ValueError("bundled package names another module")
        return ModuleManifest.from_dict(bundled_manifest(package["bundled"]))
    return verify_package(package, trust)  # the node verifies, it does not trust CORE blindly


def enabled() -> bool:
    return os.getenv("VERTEP_APPLICATION_MODULES", "false").lower() in ("true", "host")


def host_mode() -> bool:
    """No Docker in the Worker (production): the host executor runs the containers."""
    return os.getenv("VERTEP_APPLICATION_MODULES", "").lower() == "host"


def _host_dir() -> Path:
    return Path(os.getenv("VERTEP_APPLICATION_REQUEST_DIR", "/data/config/application-modules"))


def _host_state() -> dict:
    try:
        state = json.loads((_host_dir() / "state.json").read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _ports_file() -> Path:
    project = Path(__file__).resolve().parents[1]
    return Path(os.getenv("VERTEP_APPLICATION_STATE", str(project / "data/application-modules.json")))


def _ports() -> dict:
    path = _ports_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_ports(ports: dict) -> None:
    path = _ports_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(ports, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _tokens_file() -> Path:
    return _ports_file().with_name("application-module-tokens.json")


def _tokens() -> dict:
    path = _tokens_file()
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def _save_token(module_id: str, token: str) -> None:
    """The token the running container accepts, so the Worker can call it after a restart."""
    tokens = {**_tokens(), module_id: token}
    path = _tokens_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(tokens, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def local_module(module_id: str) -> tuple[str, str] | None:
    """(endpoint, module token) of an application module running and ready on this node."""
    if not enabled():
        return None
    token = _tokens().get(module_id)
    if host_mode():
        endpoint = (_host_state().get("modules", {}).get(module_id) or {}).get("endpoint")
    else:
        port = _ports().get(module_id)
        endpoint = f"http://{os.getenv('VERTEP_NODE_ADDRESS', '127.0.0.1')}:{port}" if port else None
    if not endpoint or not token:
        return None
    try:
        if httpx.get(f"{endpoint}/health", timeout=3).json().get("ready") is not True:
            return None
    except (httpx.HTTPError, ValueError):
        return None
    return endpoint, token


def _running() -> dict[str, str]:
    result = subprocess.run(["docker", "ps", "--filter", "label=vertep.module", "--format",
                             '{{.Label "vertep.module"}} {{.Label "vertep.module.version"}}'],
                            capture_output=True, text=True, timeout=60)
    running = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            running[parts[0]] = parts[1]
    return running


def _trust():
    from core.module.trust import TrustStore
    project = Path(__file__).resolve().parents[1]
    return TrustStore.from_root(Path(os.getenv("VERTEP_MODULE_TRUST_ROOT", str(project / "config/module-trust"))))


def reconcile(client: httpx.Client, core_url: str, node_name: str) -> list[dict]:
    """Converge running containers to CORE's assignment; return inventory items."""
    response = client.post(f"{core_url}/api/modules/node-assignments", json={"node_name": node_name})
    response.raise_for_status()
    trust = _trust()
    desired, packages = {}, {}
    for item in response.json().get("modules", []):
        try:
            manifest = _verified(item, trust)
        except Exception as error:
            logger.warning("Application module package refused", extra={"module": item.get("module_id"),
                                                                       "error": str(error)})
            continue
        if manifest.kind == "application" and manifest.module_id == item["module_id"]:
            desired[manifest.module_id] = manifest
            packages[manifest.module_id] = item["package"]
    if host_mode():
        return _reconcile_on_host(client, core_url, node_name, desired, packages)
    ports, running = _ports(), _running()
    base = int(os.getenv("VERTEP_APPLICATION_PORT_BASE", "28100"))
    container_core = os.getenv("VERTEP_APPLICATION_CORE_URL", core_url)
    bind_address = os.getenv("VERTEP_APPLICATION_BIND", "127.0.0.1")
    node_address = os.getenv("VERTEP_NODE_ADDRESS", "127.0.0.1")
    for module_id in set(running) - set(desired):
        containers.stop(module_id)
    inventory = []
    for module_id, manifest in sorted(desired.items()):
        if module_id not in ports:
            ports[module_id] = base + len(ports)
            _save_ports(ports)
        if running.get(module_id) != manifest.version:
            containers.stop(module_id)
            token = client.post(f"{core_url}/api/modules/token", json={"node_name": node_name, "module_id": module_id})
            if token.status_code != 200:
                continue
            containers.start(manifest, core_url=container_core, module_token=token.json()["token"],
                             host_port=ports[module_id], bind_address=bind_address)
            _save_token(module_id, token.json()["token"])
        endpoint = f"http://{node_address}:{ports[module_id]}"
        try:
            ready = httpx.get(f"{endpoint}/health", timeout=3).json().get("ready") is True
        except (httpx.HTTPError, ValueError):
            ready = False
        inventory.append({**manifest.to_dict(), "ready": ready, "endpoint": endpoint, "origin": "application"})
    return inventory


HOST_RETRY_SECONDS = 300


def _ready(endpoint: str) -> bool:
    try:
        return httpx.get(f"{endpoint}/health", timeout=3).json().get("ready") is True
    except (httpx.HTTPError, ValueError):
        return False


def _reconcile_on_host(client: httpx.Client, core_url: str, node_name: str, desired: dict,
                       packages: dict) -> list[dict]:
    """Ask the host executor for the assigned containers and report what it runs.

    A new request is written when the assignment changed, or again after
    HOST_RETRY_SECONDS while a module is not ready (failed start, or a recreated
    CORE/Worker container that left the module network)."""
    import time
    from datetime import datetime
    state = _host_state()
    started = state.get("modules", {}) if isinstance(state.get("modules"), dict) else {}
    wanted = {module_id: manifest.version for module_id, manifest in desired.items()}
    inventory = []
    for module_id, manifest in sorted(desired.items()):
        info = started.get(module_id) or {}
        endpoint = info.get("endpoint") or f"http://{containers.resource_name(module_id)}:{int(manifest.container['port'])}"
        ready = info.get("version") == manifest.version and _ready(endpoint)
        inventory.append({**manifest.to_dict(), "ready": ready, "endpoint": endpoint, "origin": "application"})
    try:
        age = time.time() - datetime.fromisoformat(state["updated_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        age = float("inf")
    changed = wanted != state.get("requested") or set(started) - set(wanted)
    stale = age >= HOST_RETRY_SECONDS and not all(item["ready"] for item in inventory)
    request_path = _host_dir() / "request.json"
    if (changed or stale) and not request_path.exists():
        modules = []
        for module_id, manifest in sorted(desired.items()):
            response = client.post(f"{core_url}/api/modules/token", json={"node_name": node_name, "module_id": module_id})
            if response.status_code != 200:
                continue
            token = response.json()["token"]
            if (started.get(module_id) or {}).get("version") != manifest.version:
                _save_token(module_id, token)  # the token the container about to start will accept
            modules.append({"module_id": module_id, "package": packages[module_id], "token": token})
        request_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = request_path.with_name(".request.json.tmp")
        temporary.write_text(json.dumps({"node_name": node_name, "modules": modules}), encoding="utf-8")
        os.chmod(temporary, 0o600)  # carries module tokens
        temporary.replace(request_path)
    return inventory
