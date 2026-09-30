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
import re
from pathlib import Path

from .node_registry import node_roles

# Services that belong exclusively to non-Core roles and therefore must never
# publish a host port.  ``core``/``proxy``/``postgres``/``redis`` are the Core
# role surface and are intentionally excluded.
WORKER_ROLE_SERVICES = {
    "worker", "comfyui", "tts", "ollama", "publisher-worker", "backup-service",
    "monitoring", "log-store", "log-collector", "grafana", "update-agent",
}

COMPOSE_FILES = ("docker-compose.yml", "deploy/docker-compose.yml",
                 "docker-compose.nvidia.yml", "docker-compose.amd.yml")

_PORTS_BLOCK = re.compile(r"^(\s+)ports:\s*\[(?P<ports>[^\]]*)\]\s*$", re.MULTILINE)
_SERVICE_KEY = re.compile(r"^  (?P<name>[A-Za-z0-9_.-]+):\s*$")


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
    violations: list[dict] = []
    lines = text.splitlines()
    current = None
    for index, line in enumerate(lines):
        match = _SERVICE_KEY.match(line)
        if match:
            current = match.group("name")
            continue
        port_match = _PORTS_BLOCK.match(line)
        if not port_match or current is None or current not in forbidden:
            continue
        for raw in port_match.group("ports").split(","):
            port = raw.strip().strip("\"'")
            if not port:
                continue
            # Loopback-bound publications are host-local, not fleet-reachable.
            if port.startswith("127.0.0.1:") or port.startswith("localhost:"):
                continue
            violations.append({"file": str(compose_path), "service": current,
                               "port": port, "line": index + 1})
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
