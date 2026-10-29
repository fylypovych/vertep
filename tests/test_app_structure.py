"""Guard-rail tests for the core/app.py structural refactoring (Issue #110).

These tests enforce the architectural boundaries the refactoring establishes:
no circular imports from ``core.api.*`` back into ``core.app``, and a bounded
file size for the composition root. They fail loudly when a new violation is
introduced.
"""
from __future__ import annotations

import re
import ast
import json
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "core"


def test_composition_root_has_no_endpoint_or_business_definitions():
    source = (CORE / "app.py").read_text(encoding="utf-8")
    assert len(source.splitlines()) <= 300
    tree = ast.parse(source)
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                   for node in tree.body)
    assignments = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))]
    assert len(assignments) == 1
    assignment = assignments[0]
    assert isinstance(assignment, ast.Assign)
    assert [target.id for target in assignment.targets] == ["app"]
    assert isinstance(assignment.value, ast.Call)
    assert isinstance(assignment.value.func, ast.Name) and assignment.value.func.id == "FastAPI"


def test_all_method_path_contracts_match_the_before_extraction_snapshot():
    from core.app import app
    before = json.loads((Path(__file__).parent / "fixtures/api-routes-before-366.json").read_text())
    after = sorted([method, route.path] for route in app.routes
                   for method in (getattr(route, "methods", None) or ["MOUNT"]))
    # #368 intentionally adds execution routes. Keep the #366 snapshot intact
    # and assert the exact union so removals and accidental additions still fail.
    execution = [
        ["POST", "/api/tasks/claim/workflow"],
        ["POST", "/api/tasks/renew/workflow"],
        ["POST", "/api/tasks/result/workflow"],
        ["GET", "/api/workflow-execution/templates"],
        ["GET", "/api/workflow-execution/{job_id}"],
        ["POST", "/api/workflow-execution/{job_id}/action"],
    ]
    assert after == sorted(before + execution)


def test_api_and_telegram_cannot_resolve_the_composition_root_at_runtime():
    for directory in (CORE / "api", CORE / "telegram"):
        for path in directory.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert all(alias.name != "core.app" for alias in node.names), path
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "core.app", path
                    assert not (node.module == "core" and any(alias.name == "app" for alias in node.names)), path
                if isinstance(node, ast.Constant):
                    assert node.value != "core.app", path
                if isinstance(node, ast.Attribute):
                    assert not (isinstance(node.value, ast.Name) and node.value.id == "sys"
                                and node.attr == "modules"), path


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
