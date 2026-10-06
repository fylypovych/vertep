"""Real Test Runner — executes real-test scenarios on a deployment.

Architecture (AGENTS.md §28): all generation dispatch goes through the
worker capability layer; CORE only orchestrates.  Health and connectivity
checks reuse ``core/health_checks.py``; node/service checks reuse
``core/node_registry``.  GitHub reporting is delegated to
``core/real_tests/github.py`` using ``gh`` CLI with the encrypted
``github_pat`` secret.

Lifecycle:
  start → run_checks → finalize → report_to_github → (close_issue on PASS)
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from typing import Any, Callable

from .github import GitHubReporter
from .models import CheckResult, CheckStatus, TestRun, TestRunStatus, utc_now
from . import scenarios as _scenarios_module
from .scenarios import CHECK_REGISTRY
from .storage import (
    append_check,
    audit_entry,
    create_test_run,
    get_test_run,
    list_pending_reports,
    record_github_report,
    update_test_run,
)
from core.logging_config import secret_redact as _log_secret_redact


def _git_commit() -> str:
    """Return the full commit SHA for deployment identity.

    Priority: GITHUB_SHA env → git rev-parse.  Returns an empty string when
    neither source yields a SHA (Issue #95): a ``VERSION`` value or the literal
    ``unknown`` is not a commit identity and must never reach acceptance.
    ``can_close_issue`` treats an empty/unknown SHA as fail-closed, so an
    unresolved identity cannot auto-close an rt Issue.
    """
    github_sha = os.getenv("GITHUB_SHA", "").strip()
    if github_sha:
        return github_sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=os.getcwd(),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _environment() -> dict[str, Any]:
    from scripts.qualify_infrastructure import environment
    return environment()


class RealTestRunner:
    """Orchestrates real-test execution, persistence, and GitHub reporting."""

    def __init__(self, reporter: GitHubReporter | None = None) -> None:
        self._reporter = reporter or GitHubReporter()
        self._lock = threading.RLock()

    # --- Lifecycle -------------------------------------------------------

    def start(
        self,
        rt_id: str,
        initiator: str = "api",
    ) -> TestRun:
        """Begin a new test run for the scenario identified by ``rt_id``."""
        scenario = _scenarios_module.find_scenario(rt_id=rt_id)
        if scenario is None:
            raise ValueError(f"Unknown rt_id: {rt_id}")

        run = create_test_run(
            rt_id=rt_id,
            rt_issue_number=scenario.get("rt_issue"),
            initiator=initiator,
            version=_scenarios_module.current_version(),
            commit_sha=_git_commit(),
            environment=_environment(),
            hardware=_scenarios_module.hardware_summary(),
            core_node_id=_scenarios_module.core_node_id(),
            targets=[node["node_id"] for node in _scenarios_module.nodes_list()],
        )
        return run

    def run_checks(self, run: TestRun, check_names: list[str] | None = None) -> TestRun:
        """Execute the registered check functions for a test run."""
        names = check_names or run_rt_check_names(run.rt_id)
        scenario = _scenarios_module.find_scenario(rt_id=run.rt_id)
        if scenario:
            mandatory_scenario_checks = set(scenario.get("checks", []))
            missing_mandatory = [n for n in mandatory_scenario_checks if n not in names]
            if missing_mandatory:
                audit_entry(run.test_run_id, "validation",
                            f"missing mandatory checks auto-added: {missing_mandatory}", "runner")
                names = list(set(names) | mandatory_scenario_checks)
        for name in names:
            check_fn = CHECK_REGISTRY.get(name)
            if check_fn is None:
                result = CheckResult(name=name, status=CheckStatus.NOT_CONFIGURED,
                                     detail=f"check '{name}' not registered", mandatory=True)
            else:
                passed, detail = _safe_execute_check(check_fn, name)
                if passed is None:
                    status = CheckStatus.SKIPPED
                elif passed:
                    status = CheckStatus.PASS
                else:
                    status = CheckStatus.FAIL
                result = CheckResult(name=name, status=status, detail=detail, mandatory=True)
            append_check(run.test_run_id, result)
            audit_entry(run.test_run_id, "check", f"{name}: {result.status.value}", "runner")
            run.checks.append(result)
        return run

    def finalize(self, run: TestRun) -> TestRun:
        """Determine the final result from mandatory checks and scenario policy.

        The success policy of every scenario is PASS-only, matching the
        ``GitHubReporter.can_close_issue`` gate: WARNING/SKIPPED/other
        statuses never produce scenario PASS.  A run whose only
        non-PASS mandatory checks are not-executed procedures finalizes as
        ``PROCEDURE`` — an explicit "not proved", never a PASS.
        """
        scenario = _scenarios_module.find_scenario(rt_id=run.rt_id)
        scenario_id = scenario.get("id", "") if scenario else ""
        success_policy = _scenarios_module.get_scenario_success_policy(scenario_id) if scenario else {CheckStatus.PASS}
        mandatory = [c for c in run.checks if c.mandatory]
        if not mandatory:
            run.final_result = "FAIL"
            run.error_details = "No mandatory checks to evaluate"
        else:
            failures = [c for c in mandatory if c.status not in success_policy]
            if not failures:
                run.final_result = "PASS"
                run.error_details = None
            elif all(c.status == CheckStatus.SKIPPED for c in failures):
                run.final_result = "PROCEDURE"
                run.error_details = "; ".join(
                    f"{c.name}: {c.detail}" for c in failures[:10]
                )
            else:
                run.final_result = "FAIL"
                run.error_details = "; ".join(f"{c.name}: {c.detail}" for c in failures[:10])

        run.status = TestRunStatus.PASS if run.final_result == "PASS" else TestRunStatus.FAIL
        run.finished_at = utc_now()
        update_test_run(run)
        audit_entry(run.test_run_id, "finalized", f"result={run.final_result}", "runner")
        return run

    def report_to_github(self, run: TestRun) -> dict[str, Any]:
        """Post the result to the corresponding GitHub rt Issue."""
        result = self._reporter.report(run)
        if result["reported"]:
            run.status = TestRunStatus.REPORTED
            run.github_report = {"reported_at": utc_now(),
                                 "comment_id": result["comment_id"],
                                 "final_result": run.final_result,
                                 "version": run.version,
                                 "commit_sha": run.commit_sha}
        else:
            run.status = TestRunStatus.REPORT_PENDING
            run.github_report = {"error": result["error"],
                                 "final_result": run.final_result}
        update_test_run(run)
        audit_entry(run.test_run_id, "github_status",
                    f"status={run.status.value} reported={result['reported']}", "runner")
        return result

    def close_rt_issue(self, run: TestRun) -> dict[str, Any]:
        """Attempt to close the rt Issue if all PASS conditions are met."""
        result = self._reporter.close_issue(run)
        if result["closed"]:
            audit_entry(run.test_run_id, "rt_issue_closed",
                        f"issue #{run.rt_issue_number} closed", "runner")
        return result

    # --- Full lifecycle --------------------------------------------------

    def run(self, rt_id: str, initiator: str = "api",
            check_names: list[str] | None = None) -> TestRun:
        """Execute the full real-test lifecycle for a scenario."""
        run = self.start(rt_id, initiator=initiator)
        audit_entry(run.test_run_id, "running", f"checks={check_names or 'default'}", initiator)

        run = self.run_checks(run, check_names=check_names)
        run = self.finalize(run)
        self.report_to_github(run)

        if run.final_result == "PASS":
            self.close_rt_issue(run)
        return get_test_run(run.test_run_id) or run

    def retry_report(self, test_run_id: str) -> TestRun | None:
        """Retry GitHub reporting for a run stuck in REPORT_PENDING."""
        run = get_test_run(test_run_id)
        if run is None:
            return None
        result = self._reporter.retry_pending(run)
        if result["reported"]:
            run.status = TestRunStatus.REPORTED
            run.github_report = {"reported_at": utc_now(),
                                 "comment_id": result["comment_id"],
                                 "final_result": run.final_result,
                                 "version": run.version,
                                 "commit_sha": run.commit_sha}
        else:
            run.github_report = {**(run.github_report or {}),
                                 "error": result.get("error"),
                                 "final_result": run.final_result,
                                 "version": run.version,
                                 "commit_sha": run.commit_sha}
        update_test_run(run)
        audit_entry(run.test_run_id, "retry_report",
                    f"reported={result['reported']}", "runner")
        return run

    def recover_pending_reports(self) -> list[dict[str, Any]]:
        """Retry all runs stuck in REPORT_PENDING after a restart."""
        results = []
        for run in list_pending_reports():
            audit_entry(run.test_run_id, "recovery_start",
                        "retrying pending report after restart", "system")
            result = self.retry_report(run.test_run_id)
            if result:
                results.append({
                    "test_run_id": result.test_run_id,
                    "reported": result.status == TestRunStatus.REPORTED,
                })
        return results

    def get_report(self, test_run_id: str) -> dict | None:
        """Build a human-readable report dict for a test run."""
        run = get_test_run(test_run_id)
        if run is None:
            return None
        return self._format_report(run)

    def _format_report(self, run: TestRun) -> dict[str, Any]:
        checks = [
            {"name": c.name, "status": c.status.value,
             "detail": _redact_secrets(c.detail),
             "mandatory": c.mandatory}
            for c in run.checks
        ]
        return {
            "test_run_id": run.test_run_id,
            "rt_id": run.rt_id,
            "rt_issue_number": run.rt_issue_number,
            "version": run.version,
            "commit": run.commit_sha,
            "result": run.final_result,
            "status": run.status.value,
            "environment": {k: _redact_secrets(str(v)) for k, v in (run.environment or {}).items()},
            "hardware": run.hardware,
            "core_node_id": run.core_node_id,
            "targets": run.targets,
            "checks": checks,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "error_details": _redact_secrets(run.error_details or ""),
            "github_report": run.github_report,
            "audit": run.audit,
        }


# --- Helpers ---------------------------------------------------------------

_SECRET_PATTERN = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|passwd|pwd|private[_-]?key|"
    r"aws_|ghp_|gho_|github_pat|bearer|authorization|credentials?|"
    r"access[_-]?key|client[_-]?secret|session[_-]?secret|jwt[_-]?secret|"
    r"encryption[_-]?key|internal[_-]?api[_-]?key)\s*[=:]\s*["
    r"'\"]?[A-Za-z0-9_\-\.]{8,}",
    re.IGNORECASE,
)

_JSON_SECRET_PATTERN = re.compile(
    r'"(api[_-]?key|token|secret|password|private[_-]?key|'
    r'credentials?|access[_-]?key|client[_-]?secret|jwt[_-]?secret|'
    r'encryption[_-]?key|internal[_-]?api[_-]?key)"\s*:\s*'
    r'"([A-Za-z0-9_\-\.]{8,})"',
    re.IGNORECASE,
)

_SECRET_KEY_RE = re.compile(
    r"(?ix)"
    r"(?P<prefix>"
    r"(?:[A-Za-z_][A-Za-z0-9_\-]*|aws_[A-Za-z0-9_\-]*|github_pat|ghp_[A-Za-z0-9_\-]*|gho_[A-Za-z0-9_\-]*)"
    r"\s*[:=]\s*"
    r")"
    r"(?P<value>"
    r"(?:\"[^\"\\]*(?:\\.[^\"\\]*)*\"|'[^'\\]*(?:\\.[^'\\]*)*'|[A-Za-z0-9_\-\./:=+]{8,})"
    r")"
)


def _redact_secrets(text: str) -> str:
    """Redact long-lived secret-shaped values while leaving short benign values intact."""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text

    text = re.sub(r"(?i)(Authorization\s*:\s*Basic\s+)([A-Za-z0-9._\-+/=]{8,})",
                  r"\1***REDACTED***", text)

    def replace_secret(match: re.Match) -> str:
        value = match.group("value").strip("\"'")
        if len(value) < 8:
            return match.group(0)
        return f"{match.group('prefix')}***REDACTED***"

    text = _SECRET_KEY_RE.sub(replace_secret, text)

    def replace_json_secret(match: re.Match) -> str:
        key = match.group("key")
        value = match.group("value").strip("\"'")
        if len(value) < 8:
            return match.group(0)
        return f'{key}: "***REDACTED***"' if match.group("quote") == '"' else f"{key}: '***REDACTED***'"

    text = re.sub(
        r"(?ix)(?P<prefix>(?:\"|')?(?P<key>api[_-]?key|apikey|token|secret|password|passwd|pwd|private[_-]?key|aws_[a-z_]*|access[_-]?key|client[_-]?secret|session[_-]?secret|jwt[_-]?secret|encryption[_-]?key|internal[_-]?api[_-]?key|authorization|credentials?)(?:\"|')?\s*:\s*)(?P<quote>\"|')?(?P<value>[A-Za-z0-9_\-\./:=+]{8,})(?P=quote)",
        replace_json_secret,
        text,
    )

    text = re.sub(r'(?i)(?P<key>token|password|secret|api[_-]?key|access[_-]?key|client[_-]?secret|credentials?|authorization|session[_-]?secret|jwt[_-]?secret|encryption[_-]?key|internal[_-]?api[_-]?key)\s*[:=]\s*\*\*\*REDACTED\*\*\*',
                  r'\g<key> = ***REDACTED***', text)
    return text


def _safe_execute_check(fn: Callable[[], Any], name: str) -> tuple[bool | None, str]:
    """Execute a check, returning (passed, detail). Never raises.

    ``passed is None`` means not-applicable/not-executed (a real procedure
    that cannot run in this environment); the runner records SKIPPED for it
    so it can never be mistaken for a PASS (Issue i.0.0.0.89).  A check that
    does not return a (passed, detail) tuple is broken and stays a failure.
    """
    try:
        result = fn()
        if result is None:
            return False, "check returned no result"
        passed, detail = result
        detail = _redact_secrets(str(detail)[:500])
        if passed is None:
            return None, detail
        return bool(passed), detail
    except Exception as exc:
        return False, _redact_secrets(f"check '{name}' raised: {exc}")


def run_rt_check_names(rt_id: str) -> list[str]:
    """Resolve the check names for a scenario by rt_id.

    Unknown scenarios resolve to an empty list — fail-closed, never to a
    generic docker/core_api probe pair.
    """
    scenario = _scenarios_module.find_scenario(rt_id=rt_id)
    if scenario is None:
        return []
    return list(scenario.get("checks") or [])
