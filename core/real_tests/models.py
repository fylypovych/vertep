"""Real Test Runner — model of real test runs, checks, and scenarios."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any
import secrets

from pydantic import BaseModel, Field

from ..version import application_version


class TestRunStatus(str, Enum):
    __test__ = False

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PASS = "PASS"
    FAIL = "FAIL"
    REPORT_PENDING = "REPORT_PENDING"
    REPORTED = "REPORTED"
    ERROR = "ERROR"


class CheckStatus(str, Enum):
    __test__ = False

    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"
    SKIPPED = "SKIPPED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class CheckResult(BaseModel):
    __test__ = False

    name: str
    status: CheckStatus
    detail: str = ""
    duration_ms: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    mandatory: bool = True


class TestRun(BaseModel):
    __test__ = False

    test_run_id: str = Field(default_factory=lambda: secrets.token_hex(16))
    rt_id: str
    rt_issue_number: int | None = None
    version: str = Field(default_factory=application_version)
    commit_sha: str = "unknown"
    status: TestRunStatus = TestRunStatus.RUNNING
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    finished_at: str | None = None
    initiator: str = "api"
    environment: dict[str, Any] = Field(default_factory=dict)
    hardware: dict[str, Any] = Field(default_factory=dict)
    core_node_id: str | None = None
    targets: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    final_result: str | None = None
    error_details: str | None = None
    github_report: dict[str, Any] | None = None
    audit: list[dict[str, str]] = Field(default_factory=list)

    def to_report(self) -> dict[str, Any]:
        return _test_run_to_report(self)


class RealTestScenario(BaseModel):
    id: str
    name: str
    rt_id: str
    rt_issue_number: int | None = None
    description: str = ""
    checks: list[str] = Field(default_factory=list)
    mandatory: bool = True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _test_run_to_report(run: TestRun) -> dict[str, Any]:
    return {
        "test_run_id": run.test_run_id,
        "rt_id": run.rt_id,
        "rt_issue_number": run.rt_issue_number,
        "version": run.version,
        "commit_sha": run.commit_sha,
        "status": run.status.value,
        "final_result": run.final_result,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "initiator": run.initiator,
        "environment": run.environment,
        "hardware": run.hardware,
        "core_node_id": run.core_node_id,
        "targets": run.targets,
        "checks": [
            {"name": c.name, "status": c.status.value, "detail": c.detail,
             "duration_ms": c.duration_ms, "evidence": c.evidence,
             "error": c.error, "mandatory": c.mandatory}
            for c in run.checks
        ],
        "error_details": run.error_details,
        "github_report": run.github_report,
    }
