"""Outbound-only connectivity gate for non-Core node roles.

Every non-Core role must reach the fleet exclusively by dialing out: the
installer, update agent and worker open outbound connections to Core, and Core
never dials a node.  An accidentally published host port on a worker-role
service silently breaks that model behind NAT/VPN and firewall setups, so the
published Compose surface is verified mechanically instead of by prose.

Only the Core role is allowed to publish inbound ports; PostgreSQL stays bound
to loopback, which is host-local and not reachable from other hosts.
"""

import os
from pathlib import Path

import yaml

from .node_registry import node_roles

# Services that belong exclusively to non-Core roles and therefore must never
# publish a host port.  ``core``/``proxy``/``postgres``/``redis`` are the Core
# role surface and are intentionally excluded.
WORKER_ROLE_SERVICES = {
    "worker", "comfyui", "tts", "stt", "lora-trainer", "musetalk", "ollama", "publisher-worker", "backup-service",
    "monitoring", "log-store", "log-collector", "grafana", "update-agent",
}

COMPOSE_FILES = ("docker-compose.yml", "deploy/docker-compose.yml",
                 "docker-compose.nvidia.yml", "docker-compose.amd.yml")

_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


def non_core_role_services() -> set[str]:
    """Every service that only non-Core roles declare, derived from the catalog."""
    services: set[str] = set()
    for role, definition in node_roles().items():
        if role == "core" or not isinstance(definition, dict):
            continue
        for service in definition.get("services", []) or []:
            if service not in {"worker", "update-agent"}:
                services.add(str(service))
    return services


def inbound_port_violations(compose_path: Path) -> list[dict]:
    """Published host ports for services that must stay outbound-only."""
    try:
        text = compose_path.read_text(encoding="utf-8")
    except OSError:
        return []
    forbidden = non_core_role_services() | WORKER_ROLE_SERVICES
    services = (yaml.safe_load(text) or {}).get("services") or {}
    violations: list[dict] = []
    for name, service in services.items():
        if name not in forbidden or not isinstance(service, dict):
            continue
        # Short ("8188:8188", inline or block list) and long (``published:``) syntax.
        for entry in service.get("ports") or []:
            if isinstance(entry, dict):
                if entry.get("published") in (None, ""):
                    continue
                host_ip = str(entry.get("host_ip") or "")
                port = f"{host_ip + ':' if host_ip else ''}{entry['published']}:{entry.get('target', '')}"
            else:
                port = str(entry).strip()
            if not port:
                continue
            # Loopback-bound publications are host-local, not fleet-reachable.
            if port.startswith(tuple(f"{address}:" for address in _LOOPBACK)):
                continue
            violations.append({"file": str(compose_path), "service": name, "port": port})
    return violations


def check_outbound_only(root: Path | None = None) -> dict:
    """Verify that no non-Core role service publishes a fleet-reachable port."""
    root = Path(root or os.getenv("VERTEP_ROOT", "."))
    checked = []
    violations: list[dict] = []
    for relative in COMPOSE_FILES:
        path = root / relative
        if not path.is_file():
            continue
        checked.append(relative)
        violations.extend(inbound_port_violations(path))
    return {
        "outbound_only": not violations,
        "checked_files": checked,
        "forbidden_services": sorted(non_core_role_services() | WORKER_ROLE_SERVICES),
        "violations": violations,
    }
