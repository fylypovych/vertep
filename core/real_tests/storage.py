"""File-based persistence for Real Test runs.

Mirrors the storage pattern from ``core/operations.py``: a thread-safe
JSON document for the active run registry plus append-only JSONL audit
logs per test run.  State lives under ``config_root()/real-tests/``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from ..atomic_write import atomic_write_json
from ..first_run import config_root
from .models import CheckResult, CheckStatus, TestRun, TestRunStatus, utc_now


_lock = threading.RLock()

_TEST_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")


def _state_dir() -> Path:
    override = os.getenv("REAL_TESTS_STORAGE_DIR")
    if override:
        return Path(override)
    return config_root() / "real-tests"


def _registry_path() -> Path:
    return _state_dir() / "test-runs.json"


def _audit_path(test_run_id: str) -> Path:
    return _state_dir() / f"{test_run_id}.audit.jsonl"


def _read_registry() -> dict[str, dict]:
    try:
        value = json.loads(_registry_path().read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    return {}


def _write_registry(runs: dict[str, dict]) -> None:
    _state_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_json(_registry_path(), runs, mode=0o600)


def _serialize(run: TestRun) -> dict[str, Any]:
    data = run.model_dump()
    data["checks"] = [c.model_dump() for c in run.checks]
    return data


def _deserialize(data: dict) -> TestRun:
    return TestRun(
        **{**data, "checks": [CheckResult(**c) for c in (data.get("checks") or [])]},
    )


def create_test_run(
    rt_id: str,
    rt_issue_number: int | None,
    initiator: str,
    version: str,
    commit_sha: str,
    environment: dict[str, Any],
    hardware: dict[str, Any],
    core_node_id: str | None,
    targets: list[str],
) -> TestRun:
    test_run_id = secrets.token_hex(16)
    now = utc_now()
    run = TestRun(
        test_run_id=test_run_id,
        rt_id=rt_id,
        rt_issue_number=rt_issue_number,
        version=version,
        commit_sha=commit_sha,
        started_at=now,
        initiator=initiator,
        environment=environment,
        hardware=hardware,
        core_node_id=core_node_id,
        targets=targets,
    )
    with _lock:
        runs = _read_registry()
        runs[test_run_id] = _serialize(run)
        _write_registry(runs)
    audit_entry(test_run_id, "created", f"rt={rt_id} issuer={initiator}", initiator)
    return run


def get_test_run(test_run_id: str) -> TestRun | None:
    if not _TEST_RUN_ID_RE.fullmatch(test_run_id):
        return None
    with _lock:
        runs = _read_registry()
    data = runs.get(test_run_id)
    if not data:
        return None
    return _deserialize(data)


def list_test_runs(limit: int = 50) -> list[TestRun]:
    with _lock:
        runs = _read_registry()
    sorted_runs = sorted(runs.values(),
                         key=lambda r: r.get("started_at") or "", reverse=True)
    return [_deserialize(r) for r in sorted_runs[:limit]]


def update_test_run(run: TestRun) -> None:
    with _lock:
        runs = _read_registry()
        exists = runs.get(run.test_run_id)
        if exists is None:
            runs[run.test_run_id] = _serialize(run)
        else:
            merged = _deserialize(exists)
            merged.status = run.status
            merged.finished_at = run.finished_at
            merged.checks = run.checks
            merged.final_result = run.final_result
            merged.error_details = run.error_details
            merged.github_report = run.github_report
            merged.audit = run.audit
            runs[run.test_run_id] = _serialize(merged)
        _write_registry(runs)


def delete_test_run(test_run_id: str) -> bool:
    if not _TEST_RUN_ID_RE.fullmatch(test_run_id):
        return False
    with _lock:
        runs = _read_registry()
        if test_run_id not in runs:
            return False
        del runs[test_run_id]
        _write_registry(runs)
    _audit_path(test_run_id).unlink(missing_ok=True)
    return True


def audit_entry(
    test_run_id: str,
    phase: str,
    message: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    entry = {
        "test_run_id": test_run_id,
        "phase": phase,
        "timestamp": utc_now(),
        "message": message or "",
        "actor": actor,
    }
    try:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with _audit_path(test_run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    with _lock:
        runs = _read_registry()
        run_data = runs.get(test_run_id)
        if run_data is not None:
            audit_list = run_data.setdefault("audit", [])
            audit_list.append(entry)
            runs[test_run_id] = run_data
            _write_registry(runs)
    return entry


def append_check(
    test_run_id: str,
    check: CheckResult,
) -> None:
    with _lock:
        runs = _read_registry()
        run_data = runs.get(test_run_id)
        if run_data is None:
            return
        checks = run_data.setdefault("checks", [])
        checks.append(check.model_dump())
        runs[test_run_id] = run_data
        _write_registry(runs)


def record_github_report(
    test_run_id: str,
    result: str | None,
    comment_id: str | None,
    error: str | None = None,
) -> None:
    now = utc_now()
    report: dict[str, Any] = {
        "reported_at": now,
        "final_result": result,
        "comment_id": comment_id,
        "error": error,
    }
    with _lock:
        runs = _read_registry()
        run_data = runs.get(test_run_id)
        if run_data is None:
            return
        existing = run_data.get("github_report")
        if existing and isinstance(existing, dict):
            existing.update(report)
        else:
            run_data["github_report"] = report
        runs[test_run_id] = run_data
        _write_registry(runs)
