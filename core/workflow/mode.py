"""Operator choice between the static pipeline and configured dynamic workflows."""

import json
import os
from pathlib import Path

from core.first_run import config_root

STATIC = "static"
DYNAMIC = "dynamic"
_FILE = "workflow-mode.json"


def _stored_mode() -> str | None:
    try:
        return json.loads((config_root() / _FILE).read_text(encoding="utf-8")).get("mode")
    except (OSError, ValueError, AttributeError):
        return None


def dynamic_definitions_root() -> str | None:
    """The definitions root, unless dynamic workflows are off.

    An installation without ``WORKFLOW_DEFINITIONS_ROOT`` stays static; an operator's
    explicit static choice overrides a configured root.
    """
    root = os.getenv("WORKFLOW_DEFINITIONS_ROOT")
    return root if root and _stored_mode() != STATIC else None


def _has_definitions() -> bool:
    root = os.getenv("WORKFLOW_DEFINITIONS_ROOT")
    return bool(root) and any(path.suffix in {".yaml", ".json"} for path in Path(root).glob("*"))


def active_dynamic_jobs() -> int:
    """Workflow jobs that would stall in static mode; legacy-path jobs keep running there."""
    if not dynamic_definitions_root():
        return 0
    from .runtime import workflow_runtime
    runtime = workflow_runtime()
    engine = getattr(runtime, "engine", None)
    if engine is None:
        return 0
    with engine._lock:
        return sum(1 for job in engine._jobs.values()
                   if job.get("execution_mode") != "legacy"
                   and job.get("status") not in {"COMPLETED", "CANCELLED", "FAILED"})


def workflow_mode_state() -> dict:
    return {"mode": DYNAMIC if dynamic_definitions_root() else STATIC,
            "default": STATIC, "dynamic_available": _has_definitions(),
            "active_dynamic_jobs": active_dynamic_jobs()}


def set_workflow_mode(mode: str) -> dict:
    if mode not in {STATIC, DYNAMIC}:
        raise ValueError("Unknown workflow mode")
    if mode == DYNAMIC and not _has_definitions():
        raise ValueError("Dynamic workflow has no configured definitions")
    active = active_dynamic_jobs() if mode == STATIC else 0
    if active:
        raise ValueError(f"Спершу завершіть або скасуйте завдання динамічного режиму: {active}")
    root = config_root()
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".{_FILE}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    temporary.replace(root / _FILE)
    return workflow_mode_state()
