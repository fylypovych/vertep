"""Real Test scenario catalog and check registry.

Scenarios are loaded from ``scripts/qualify_infrastructure.py`` SCENARIOS
(each has an ``rt`` field pointing to a machine-executable procedure and
an ``rt_issue`` GitHub issue number).

The check registry maps check names to callable executors.  Checks reuse
``core/health_checks.py`` for infrastructure probes and ``core/node_registry``
for node/service verification.  Generation-heavy checks are procedure-based
(PROCEDURE status when not run on real hardware); dispatching a real task
is delegated to the worker capability dispatch, never to a direct provider
call in CORE (see AGENTS.md §4.1).
"""

from __future__ import annotations

import os
from typing import Any, Callable

from ..health_checks import (
    check_comfyui,
    check_core_api,
    check_docker,
    check_ollama,
    check_postgres,
    check_redis,
)
from ..node_registry import registered_nodes
from ..version import application_version


CheckFn = Callable[[], tuple[bool, str]]


def _check_node_connectivity() -> tuple[bool, str]:
    nodes = registered_nodes()
    if not nodes:
        role = os.getenv("NODE_ROLE", "core")
        if role == "core":
            return True, "no remote nodes expected on core-only"
        return False, "no registered nodes"
    revoked = [n["node_id"] for n in nodes if (n.get("status") or "") == "REVOKED"]
    return len(revoked) == 0, f"{len(nodes)} nodes registered, {len(revoked)} revoked"


def _check_certificate_validation() -> tuple[bool, str]:
    nodes = registered_nodes()
    pending = [n["node_id"] for n in nodes if not n.get("certificate_serial")]
    if pending:
        return False, f"nodes without certificate: {pending}"
    return True, f"all {len(nodes)} nodes have certificates"


def _check_worker_enrollment() -> tuple[bool, str]:
    nodes = registered_nodes()
    online = [n for n in nodes if n.get("runtime_status") in {"ONLINE", "READY", "FREE", "BUSY"}]
    if not nodes:
        return True, "no remote nodes required on this role"
    return len(online) > 0, f"{len(online)}/{len(nodes)} nodes online"


def _check_health_core() -> tuple[bool, str]:
    from ..health_checks import health_status, run_checks
    checks = run_checks(role=os.getenv("NODE_ROLE", "core"))
    status = health_status(checks)
    return status == "HEALTHY", status


CHECK_REGISTRY: dict[str, CheckFn] = {
    "docker": check_docker,
    "postgres": check_postgres,
    "redis": check_redis,
    "core_api": check_core_api,
    "ollama": check_ollama,
    "comfyui": check_comfyui,
    "node_connectivity": _check_node_connectivity,
    "certificate_validation": _check_certificate_validation,
    "worker_enrollment": _check_worker_enrollment,
    "health_core": _check_health_core,
}


def available_checks() -> list[str]:
    return sorted(CHECK_REGISTRY.keys())


def get_check(name: str) -> CheckFn | None:
    return CHECK_REGISTRY.get(name)


def load_scenarios() -> list[dict]:
    """Load scenarios from qualify_infrastructure with resolved check lists."""
    from scripts.qualify_infrastructure import SCENARIOS
    scenarios = []
    for scenario in SCENARIOS:
        entry = dict(scenario)
        entry["checks"] = _scenario_checks(entry.get("id", ""))
        scenarios.append(entry)
    return scenarios


def _scenario_checks(scenario_id: str) -> list[str]:
    mapping = {
        "S01": ["docker", "core_api", "health_core"],
        "S02": ["docker", "core_api", "node_connectivity",
                "certificate_validation", "worker_enrollment"],
        "S03": ["docker", "comfyui", "core_api"],
        "S04": ["docker", "postgres", "redis", "core_api"],
        "S05": ["docker", "core_api"],
        "S06": ["docker", "core_api", "health_core"],
        "S07": ["docker", "core_api"],
        "S08": ["docker", "core_api"],
    }
    return mapping.get(scenario_id, ["docker", "core_api"])


def find_scenario(rt_id: str | None = None, rt_issue_number: int | None = None) -> dict | None:
    """Lookup a scenario by rt_id (e.g. 'rt::S01') or GitHub issue number.

    The returned dict includes the resolved ``checks`` list.
    """
    from scripts.qualify_infrastructure import SCENARIOS
    for scenario in SCENARIOS:
        if rt_id and scenario.get("rt", "").split()[0] == rt_id:
            return dict(scenario, checks=_scenario_checks(scenario.get("id", "")))
        if rt_issue_number and scenario.get("rt_issue") == rt_issue_number:
            return dict(scenario, checks=_scenario_checks(scenario.get("id", "")))
    return None


def hardware_summary() -> dict[str, Any]:
    from ..first_run import runtime_hardware
    return runtime_hardware()


def core_node_id() -> str | None:
    from ..first_run import installation
    return installation().get("installation_id")


def current_version() -> str:
    return application_version()


def nodes_list() -> list[dict]:
    """Wrapper for registered_nodes used by the runner."""
    return registered_nodes()
