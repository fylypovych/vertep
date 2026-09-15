"""Real Test Runner API routes.

Provides endpoints for listing available real-test scenarios, launching
test runs, retrieving results, and retrying GitHub reporting.  All
mutation routes require administrator role and the ``real_test``
operation to be allowed by the current system state.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..real_tests.runner import RealTestRunner
from ..real_tests.scenarios import available_checks, load_scenarios
from ..real_tests.storage import get_test_run, list_test_runs
from ..system_state import operation_allowed
from ..version import application_version


router = APIRouter()
_runner: RealTestRunner | None = None


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
    runner = _get_runner()
    try:
        run = runner.start(payload.rt_id, initiator="api")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run = runner.run_checks(run, check_names=payload.check_names)
    run = runner.finalize(run)
    runner.report_to_github(run)
    if run.final_result == "PASS" and payload.confirm_destructive:
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
         "finished_at": r.finished_at}
        for r in runs
    ]}


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
