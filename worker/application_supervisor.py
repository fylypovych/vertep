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

import httpx

from core.module.packages import verify_package
from worker import application_container as containers


def enabled() -> bool:
    return os.getenv("VERTEP_APPLICATION_MODULES", "false").lower() == "true"


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
    desired = {}
    for item in response.json().get("modules", []):
        try:
            manifest = verify_package(item["package"], trust)  # the node verifies, it does not trust CORE blindly
        except Exception:
            continue
        if manifest.kind == "application" and manifest.module_id == item["module_id"]:
            desired[manifest.module_id] = manifest
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
        endpoint = f"http://{node_address}:{ports[module_id]}"
        try:
            ready = httpx.get(f"{endpoint}/health", timeout=3).json().get("ready") is True
        except (httpx.HTTPError, ValueError):
            ready = False
        inventory.append({**manifest.to_dict(), "ready": ready, "endpoint": endpoint, "origin": "application"})
    return inventory
