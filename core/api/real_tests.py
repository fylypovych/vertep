"""Real Test Runner API routes.

Provides endpoints for listing available real-test scenarios, launching
test runs, retrieving results, and retrying GitHub reporting.  All
mutation routes require administrator role and the ``real_test``
operation to be allowed by the current system state.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..real_tests.runner import DESTRUCTIVE_CHECKS, RealTestRunner
from ..real_tests.scenarios import available_checks, find_scenario, load_scenarios
from ..real_tests.storage import get_test_run, list_test_runs, update_test_run
from ..system_state import operation_allowed
from ..version import application_version


router = APIRouter()
_runner: RealTestRunner | None = None

# Shared with core.real_tests.runner (single source of truth, i.0.0.0.96).
_DESTRUCTIVE_CHECKS = DESTRUCTIVE_CHECKS


class RunRequest(BaseModel):
    rt_id: str = Field(min_length=1, max_length=128)
    check_names: list[str] | None = None
    confirm_destructive: bool = False


def _get_runner() -> RealTestRunner:
    global _runner
    if _runner is None:
        _runner = RealTestRunner()
    return _runner


def _check_operation() -> None:
    if not operation_allowed("real_test"):
        raise HTTPException(
            status_code=423,
            detail="Operation 'real_test' is blocked by the current system state",
        )


@router.get("/api/real-tests/scenarios")
def scenarios():
    _check_operation()
    loaded = load_scenarios()
    return {
        "scenarios": [
            {
                "id": s["id"],
                "name": s["name"],
                "rt_id": s.get("rt", ""),
                "rt_issue": s.get("rt_issue"),
                "description": s.get("description", ""),
                "checks": s.get("checks", []),
            }
            for s in loaded
        ],
        "available_checks": available_checks(),
        "version": application_version(),
    }


@router.post("/api/real-tests/run")
def run_test(payload: RunRequest, request: Request):
    _check_operation()
    if not payload.confirm_destructive:
        raise HTTPException(
            status_code=403,
            detail="Destructive confirmation required; set confirm_destructive=true",
        )
    import base64 as _b64
    import binascii as _binascii
    from core.security import _authenticate_user as _auth_user, _valid_session as _valid_sess
    actor = "unknown"
    _identity = _valid_sess(request.cookies.get("vertep_session", ""))
    if _identity:
        actor = _identity[0]
    else:
        try:
            _scheme, _encoded = (request.headers.get("authorization", "") or "").split(" ", 1)
            _user, _supplied = _b64.b64decode(_encoded).decode().split(":", 1)
            if _scheme.lower() == "basic" and _auth_user(_user, _supplied):
                actor = _user
        except (ValueError, UnicodeError, _binascii.Error):
            pass
    runner = _get_runner()
    try:
        run = runner.start(payload.rt_id, initiator=actor)
    except ValueError as exc:
        # Issue #84: a Runner error may embed provider credentials, so the API
        # response must not echo the raw exception text.
        from ..logging_config import secret_redact
        raise HTTPException(status_code=404, detail=secret_redact(str(exc))) from exc
    # i.0.0.0.96: persist the pre-execution confirmation with the run.
    from ..real_tests.models import utc_now as _utc_now
    from ..real_tests.storage import audit_entry as _audit
    run.destructive_confirmation = bool(payload.confirm_destructive)
    run.confirmation_timestamp = _utc_now()
    update_test_run(run)
    _audit(run.test_run_id, "confirmed",
           "pre-execution confirmation by {}".format(actor), actor)
    run = runner.run_checks(run, check_names=payload.check_names)
    run = runner.finalize(run)
    runner.report_to_github(run)
    runner.close_rt_issue(run)
    return {"test_run_id": run.test_run_id, "status": run.status.value,
            "result": run.final_result, "rt_id": run.rt_id}


@router.get("/api/real-tests/runs")
def list_runs(limit: int = 50):
    _check_operation()
    runs = list_test_runs(limit=max(1, min(limit, 500)))
    return {"runs": [
        {"test_run_id": r.test_run_id, "rt_id": r.rt_id, "rt_issue_number": r.rt_issue_number,
         "version": r.version, "commit_sha": r.commit_sha, "status": r.status.value,
         "final_result": r.final_result, "started_at": r.started_at,
         "finished_at": r.finished_at, "initiator": r.initiator,
         "progress": r.progress, "prerequisites": r.prerequisites,
         "destructive_confirmation": r.destructive_confirmation,
         "confirmation_timestamp": r.confirmation_timestamp}
        for r in runs
    ]}


@router.get("/api/real-tests/scenarios/{rt_id}/prerequisites")
def scenario_prerequisites(rt_id: str):
    """i.0.0.0.96: describe what a run will actually execute before the user
    confirms it, so destructive checks are visible up front."""
    _check_operation()
    from ..real_tests.scenarios import find_scenario
    scenario = find_scenario(rt_id=rt_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Unknown rt_id")
    checks = scenario.get("checks", [])
    destructive = [c for c in checks if c in _DESTRUCTIVE_CHECKS]
    return {
        "rt_id": rt_id,
        "name": scenario.get("name", ""),
        "description": scenario.get("description", ""),
        "checks": checks,
        "mandatory_checks": checks,
        "destructive_checks": destructive,
        "requires_confirmation": bool(destructive),
    }


@router.get("/api/real-tests/runs/{test_run_id}")
def get_run(test_run_id: str):
    _check_operation()
    run = get_test_run(test_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Test run not found")
    return _get_runner().get_report(test_run_id)


@router.post("/api/real-tests/runs/{test_run_id}/retry-report")
def retry_report(test_run_id: str, request: Request):
    _check_operation()
    runner = _get_runner()
    run = runner.retry_report(test_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Test run not found")
    return {"test_run_id": run.test_run_id, "status": run.status.value,
            "github_report": run.github_report}
