"""Actual runtime status for every node role contract.

The role catalog in ``config/node_roles.json`` only *declares* what a role is.
Fleet control and the Web UI must instead see whether the role is actually
running right now, so every role contract carries a measured ``runtime_status``
derived from real evidence:

* the local role, from its own health checks (and the local deployment
  inventory when the role is applied on this installation);
* remote roles, from the durable self-test outcome of every enrolled node with
  that role plus live heartbeat state.

The derivation is fail-closed: no evidence means ``UNKNOWN``/``OFFLINE`` and
never ``READY``.  ``READY`` requires at least one verified runtime.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .node_registry import node_roles, registered_nodes
from .state import store

ROLE_RUNTIME_STATUSES = ("READY", "DEGRADED", "OFFLINE", "UNKNOWN")

# A runtime_status reported by a node self-test or heartbeat that proves the
# node is live and able to serve work.
_LIVE = {"ONLINE", "READY", "FREE", "BUSY"}


def role_runtime_status(role: str) -> dict:
    """Return the measured runtime status of a single role contract."""
    evidence: dict = {"source": None, "nodes": [], "local": None}
    if role == _local_role():
        evidence["source"] = "local"
        evidence["local"] = _local_evidence()
        return {"runtime_status": _local_status(evidence["local"]), "evidence": evidence}

    status, nodes = _remote_status(role)
    evidence["source"] = "nodes"
    evidence["nodes"] = nodes
    return {"runtime_status": status, "evidence": evidence}


def role_runtime_matrix() -> list[dict]:
    """Runtime status for every declared role contract, including ``core``."""
    matrix = []
    for role in node_roles():
        measured = role_runtime_status(role)
        matrix.append({"role": role, **measured})
    return matrix


def role_runtime_index() -> dict[str, str]:
    """Compact ``{role: runtime_status}`` map used by dispatch and the API."""
    return {item["role"]: item["runtime_status"] for item in role_runtime_matrix()}


def locally_deployed_roles() -> set[str]:
    """Roles this installation actually runs, from the applied deployment plan."""
    from .first_run import config_root

    try:
        plan = json.loads((Path(config_root()) / "deployment-plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(plan, dict) or not plan.get("role"):
        return set()
    roles = {str(plan["role"])}
    roles.update(str(item) for item in plan.get("additional_roles", []) if isinstance(item, str))
    return roles


def role_blocks_dispatch(role: str) -> bool:
    """True when a locally deployed role contract is measured as not ready.

    Dispatch stays capability-driven, but a role that this installation claims
    to run through an additional role while its own runtime is measured
    ``OFFLINE``/``DEGRADED`` must not keep attracting work.  Fail-open for
    unmeasured roles: a role with no evidence is not treated as broken here,
    because the per-node self-test guard is the authoritative runtime gate.
    """
    if role not in locally_deployed_roles():
        return False
    return role_runtime_status(role)["runtime_status"] in {"OFFLINE", "DEGRADED"}


def _local_role() -> str:
    return str(os.getenv("NODE_ROLE", "core"))


def _local_status(local: dict | None) -> str:
    if not local:
        return "UNKNOWN"
    if local.get("failed_checks"):
        return "DEGRADED"
    if local.get("services_total") is None:
        return "UNKNOWN"
    if not local.get("services_total"):
        # Core-only modules (PostgreSQL/Redis/web UI) are checked directly; a
        # non-Compose runtime still has a verified API surface.
        return "READY" if local.get("checks_ok") else "DEGRADED"
    if local.get("services_missing") or local.get("services_unhealthy"):
        return "DEGRADED"
    return "READY"


def _local_evidence() -> dict | None:
    """Health checks of the local role plus, when present, its container set."""
    from .health_checks import health_status, run_checks

    try:
        checks = run_checks(_local_role())
    except Exception:
        return None
    names = [name for name, value in checks.items() if isinstance(value, tuple)]
    evidence = {
        "checked_at": checks.get("checked_at"),
        "checks_ok": health_status(checks) == "HEALTHY",
        "failed_checks": [name for name in names if checks[name][0] is False],
        "services_total": None,
        "services_missing": [],
        "services_unhealthy": [],
    }
    inventory = _deployment_inventory()
    if inventory is not None:
        containers = inventory.get("containers") or []
        evidence["services_total"] = len(containers)
        evidence["services_missing"] = sorted(
            set(inventory.get("services") or []) - {item.get("service") for item in containers})
        evidence["services_unhealthy"] = sorted(
            item.get("service", "?") for item in containers
            if item.get("state") != "running" and item.get("service") != "migrate")
    return evidence


def _deployment_inventory() -> dict | None:
    from .first_run import config_root

    path = Path(os.getenv("RUNTIME_INVENTORY_PATH", str(Path(config_root()) / "runtime-inventory.json")))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _remote_status(role: str) -> tuple[str, list[dict]]:
    """Aggregate the durable runtime status of every node enrolled for a role."""
    try:
        records = registered_nodes()
    except Exception:
        return "UNKNOWN", []
    now = datetime.now(timezone.utc)
    timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "45"))
    workers = {}
    try:
        for worker in store.load_workers():
            workers[worker.get("node_id") or worker.get("node_name")] = worker
    except Exception:
        workers = {}

    nodes: list[dict] = []
    live = 0
    revoked = 0
    for record in records:
        if record.get("role") != role:
            continue
        node_id = record.get("node_id")
        if record.get("revoked_at") or record.get("status") == "REVOKED":
            revoked += 1
            nodes.append({"node_id": node_id, "runtime_status": "REVOKED", "live": False})
            continue
        runtime_status = record.get("runtime_status") or "PENDING_SELF_TEST"
        heartbeat = workers.get(node_id)
        online = _heartbeat_fresh(heartbeat, now, timeout)
        node_live = runtime_status in _LIVE and online
        if node_live:
            live += 1
        nodes.append({
            "node_id": node_id,
            "runtime_status": runtime_status,
            "heartbeat": "ONLINE" if online else "OFFLINE",
            "live": node_live,
            "last_self_test_at": record.get("last_self_test_at"),
        })
    if live:
        return "READY", nodes
    if not nodes or (live == 0 and revoked == len(nodes)):
        return "OFFLINE" if nodes else "UNKNOWN", nodes
    return "DEGRADED", nodes


def _heartbeat_fresh(worker: dict | None, now: datetime, timeout: int) -> bool:
    if not worker or worker.get("status") in {"OFFLINE", "ERROR", "REVOKED"}:
        return False
    try:
        last_seen = datetime.fromisoformat(str(worker["last_seen"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    return (now - last_seen).total_seconds() <= timeout
