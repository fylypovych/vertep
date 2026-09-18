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
    check_gpu,
    check_ollama,
    check_postgres,
    check_redis,
    check_tts,
    check_publisher,
    check_backup,
    check_monitoring,
    check_tcp,
)
from ..node_registry import registered_nodes
from ..version import application_version
from .models import CheckStatus


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


def _check_postgres_tcp() -> tuple[bool, str]:
    """Real TCP connectivity check for PostgreSQL (not just env presence)."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return None, "not-applicable: DATABASE_URL not configured"
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    return check_tcp(host, port)


def _check_redis_tcp() -> tuple[bool, str]:
    """Real TCP connectivity check for Redis."""
    url = os.getenv("REDIS_URL", "")
    if not url:
        return None, "not-applicable: REDIS_URL not configured"
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    return check_tcp(host, port)


def _check_ollama_probe() -> tuple[bool, str]:
    """Real HTTP probe for Ollama availability."""
    return check_ollama()


def _check_comfyui_probe() -> tuple[bool, str]:
    """Real HTTP probe for ComfyUI availability."""
    return check_comfyui()


def _check_gpu_probe() -> tuple[bool, str]:
    """Real GPU detection probe."""
    return check_gpu()


def _check_tts_probe() -> tuple[bool, str]:
    """Real HTTP probe for TTS service."""
    return check_tts()


def _check_publisher_probe() -> tuple[bool, str]:
    """Real HTTP probe for Publisher service."""
    return check_publisher()


def _check_backup_writable() -> tuple[bool, str]:
    """Verify backup directory is writable."""
    return check_backup()


def _check_monitoring_probe() -> tuple[bool, str]:
    """Real HTTP probe for Prometheus monitoring."""
    return check_monitoring()


CHECK_REGISTRY: dict[str, CheckFn] = {
    "docker": check_docker,
    "postgres": check_postgres,
    "postgres_tcp": _check_postgres_tcp,
    "redis": check_redis,
    "redis_tcp": _check_redis_tcp,
    "core_api": check_core_api,
    "ollama": check_ollama,
    "ollama_probe": _check_ollama_probe,
    "comfyui": check_comfyui,
    "comfyui_probe": _check_comfyui_probe,
    "gpu": _check_gpu_probe,
    "tts": _check_tts_probe,
    "publisher": _check_publisher_probe,
    "backup": _check_backup_writable,
    "monitoring": _check_monitoring_probe,
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
        "S03": ["docker", "core_api", "comfyui_probe", "gpu"],
        "S04": ["docker", "postgres_tcp", "redis_tcp", "core_api"],
        "S05": ["docker", "core_api", "ollama_probe"],
        "S06": ["docker", "core_api", "health_core", "monitoring"],
        "S07": ["docker", "core_api", "tts", "publisher"],
        "S08": ["docker", "core_api", "backup"],
    }
    return mapping.get(scenario_id, ["docker", "core_api"])


_SCENARIO_SUCCESS_POLICY: dict[str, set] = {
    "S01": {CheckStatus.PASS},
    "S02": {CheckStatus.PASS},
    "S03": {CheckStatus.PASS, CheckStatus.WARNING},
    "S04": {CheckStatus.PASS},
    "S05": {CheckStatus.PASS},
    "S06": {CheckStatus.PASS, CheckStatus.WARNING},
    "S07": {CheckStatus.PASS, CheckStatus.WARNING, CheckStatus.SKIPPED},
    "S08": {CheckStatus.PASS, CheckStatus.WARNING, CheckStatus.SKIPPED,
            CheckStatus.NOT_CONFIGURED},
}


def get_scenario_success_policy(scenario_id: str) -> set:
    return _SCENARIO_SUCCESS_POLICY.get(scenario_id, {CheckStatus.PASS})


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
