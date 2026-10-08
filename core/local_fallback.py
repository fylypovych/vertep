"""CORE local-execution policy for Issue i.0.0.1.4 (#104).

``LOCAL_WORKER_FALLBACK`` exists only for local dev/demo: a CORE-only
developer run without separate Worker processes.  Production acceptance
with the fallback enabled is invalid (AGENTS.md §3.1).

The gate is explicit and fail-closed:

- The fallback is active only when ``LOCAL_WORKER_FALLBACK=true`` **and**
  ``VERTEP_DEMO=true``.  Setting only the former is not enough.
- Any other combination disables local execution: CORE dispatches to
  Workers via the task queue and refuses to execute generation locally.
"""

from __future__ import annotations

import os


def local_fallback_allowed() -> bool:
    """Return True only for an explicit local dev/demo run."""
    fallback = os.getenv("LOCAL_WORKER_FALLBACK", "false").lower() == "true"
    demo = os.getenv("VERTEP_DEMO", "false").lower() == "true"
    return fallback and demo


def require_no_local_execution(context: str) -> None:
    """Raise if local execution is attempted outside dev/demo.

    Called on fallback paths before any generation/publish/render runs
    locally in CORE, so a misconfigured production host fails loudly
    instead of silently generating content in CORE.
    """
    if not local_fallback_allowed():
        raise RuntimeError(
            f"Local {context} execution is disabled: "
            "LOCAL_WORKER_FALLBACK requires VERTEP_DEMO=true "
            "(dev/demo only; production acceptance with fallback is invalid)"
        )
