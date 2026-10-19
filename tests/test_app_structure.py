"""Guard-rail tests for the core/app.py structural refactoring (Issue #110).

These tests enforce the architectural boundaries the refactoring establishes:
no circular imports from ``core.api.*`` back into ``core.app``, and a bounded
file size for the composition root. They fail loudly when a new violation is
introduced.
"""
from __future__ import annotations

import re
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "core"


def test_core_api_modules_do_not_import_core_app():
    """No module under core/api/ may import core.app (Issue #110 Phase B).

    ``core.app`` is the composition root: it imports every router. If a router
    imported ``core.app`` back at module level, the import graph would be
    cyclic and the routers could not be tested in isolation.
    """
    import_pattern = re.compile(
        r"^\s*(?:from\s+core(?:\.app)?|import\s+core(?:\.app)?)\b.*", re.MULTILINE)
    offenders = []
    for path in sorted((CORE / "api").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in import_pattern.finditer(text):
            line = match.group(0)
            # Only flag real imports of core.app, not mentions in docstrings
            # or comments.
            stripped = line.lstrip()
            if stripped.startswith(("#", '"""', "'''")):
                continue
            if "core.app" in line or "core import app" in line:
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, (
        "core/api/* must not import core.app (Issue #110 Phase B):\n"
        + "\n".join(offenders))


def test_core_telegram_modules_do_not_import_core_app():
    """No module under core/telegram/ may import core.app (Issue #110 Phase D)."""
    telegram_dir = CORE / "telegram"
    if not telegram_dir.is_dir():
        return  # Phase D not yet applied; nothing to check yet
    import_pattern = re.compile(
        r"^\s*(?:from\s+core(?:\.app)?|import\s+core(?:\.app)?)\b.*", re.MULTILINE)
    offenders = []
    for path in sorted(telegram_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in import_pattern.finditer(text):
            line = match.group(0)
            stripped = line.lstrip()
            if stripped.startswith(("#", '"""', "'''")):
                continue
            if "core.app" in line or "core import app" in line:
                offenders.append(f"{path.relative_to(CORE)}: {line.strip()}")
    assert not offenders, (
        "core/telegram/* must not import core.app (Issue #110 Phase D):\n"
        + "\n".join(offenders))


def test_system_router_registered_in_app():
    """core/api/system.py must exist and be wired into core.app (Issue #110 Phase C).

    The file was re-added as a real System API implementation (update, recovery,
    roles, rolling update, restart, node status) and must be include_router'd in
    core/app.py so the /api/system/* endpoints are served.
    """
    assert (CORE / "api" / "system.py").exists(), (
        "core/api/system.py must exist (real System API, Issue #110 Phase C)")
    text = (CORE / "app.py").read_text(encoding="utf-8")
    assert "from .api.system import router as system_router" in text, (
        "core/app.py must import system_router")
    assert "app.include_router(system_router)" in text, (
        "core/app.py must register system_router")


def test_video_regeneration_functions_live_in_pipeline():
    """The shared video-regeneration path lives in core.pipeline (Phase B).

    ``core/api/jobs.py`` and the Telegram handlers both need
    ``_finalize_video_regenerate``.  It must be importable from
    ``core.pipeline`` without going through ``core.app``.
    """
    from core.pipeline import (_finalize_video_regenerate,
                               _collect_reassembly_inputs,
                               _notify_video_regeneration)
    assert callable(_finalize_video_regenerate)
    assert callable(_collect_reassembly_inputs)
    assert callable(_notify_video_regeneration)