"""GitHub integration for Real Test reporting.

Posts idempotent comments to the GitHub ``rt`` Issue that corresponds to
a completed test run.  Uses the ``gh`` CLI (same pattern as
``scripts/release.py``) — credentials are supplied via the encrypted
integration secret ``github_pat``, never hardcoded.

Resilience:
- Transient failures (network, rate-limit, 5xx) are retried with
  exponential backoff.
- If GitHub is unreachable the reporter records ``REPORT_PENDING``
  locally via the storage layer so the result is never lost.
- Idempotent: a ``test_run_id`` is reported only once; re-running the
  report for an already-reported run is a no-op.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any

from ..first_run import integration_secret_status
from .models import CheckStatus, TestRun
from .storage import audit_entry, record_github_report, update_test_run

_REPORT_MARKER = "REAL-TEST-RUN:"
_IDEMPOTENCY_RE = re.compile(rf"{_REPORT_MARKER}([0-9a-f]{{32}})")


def _is_configured() -> bool:
    return integration_secret_status().get("github_pat", False)


def _gh_env() -> dict[str, str]:
    """Return a process environment with GH_TOKEN from the secret store.

    Raises RuntimeError if the secret store is expected but unreadable,
    so callers do not silently fall back on ambient credentials.
    """
    from ..first_run import integration_secret_status
    env = dict(os.environ)
    if integration_secret_status().get("github_pat", False):
        try:
            from ..first_run import _read_encrypted_secrets
            store = _read_encrypted_secrets()
            token = store.get("github_pat", "")
            if token:
                env["GH_TOKEN"] = token
            else:
                raise RuntimeError("github_pat secret is empty in the store")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Failed to read github_pat secret: {exc}"
            ) from exc
    env.setdefault("GITHUB_REPOSITORY", "")
    return env


def _gh(repo: str | None, *args: str, timeout: int = 30) -> str:
    """Run `gh` against an explicitly specified repository.

    Raises ``_TransientError`` on transient failures and ``RuntimeError``
    on permanent failures.  Returncode is always checked.
    """
    if repo is None:
        raise RuntimeError(
            "GitHub repository not configured; set GITHUB_REPOSITORY environment variable"
        )
    cmd = ["gh", "--repo", repo, *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_gh_env(),
    )
    stdout = result.stdout.strip() if result.stdout else ""
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if any(kw in stderr.lower() for kw in
               ("rate limit", "timeout", "server error", "connection",
                "502", "503", "504", "network", "temporary")):
            raise _TransientError(f"gh failed (rc={result.returncode}): {stderr}")
        raise RuntimeError(f"gh failed (rc={result.returncode}): {stderr}")
    return stdout


def _gh_json(repo: str | None, *args: str, timeout: int = 30) -> dict:
    raw = _gh(repo, *args, timeout=timeout)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}


class GitHubReporter:
    """Publishes test-run results to the corresponding GitHub ``rt`` Issue."""

    MAX_RETRIES = 3
    BASE_DELAY = 2.0

    def report(self, run: TestRun) -> dict[str, Any]:
        """Post the test report comment to the rt Issue.

        Returns ``{"reported": bool, "comment_id": str | None, "error": str | None}``.
        On persistent failure the error is stored locally for later retry.
        """
        if run.rt_issue_number is None:
            return {"reported": False, "comment_id": None,
                    "error": "No rt_issue_number configured for this scenario"}

        if not _is_configured():
            return {"reported": False, "comment_id": None,
                    "error": "github_pat is not configured"}

        if self._already_reported(run.test_run_id, run.rt_issue_number):
            audit_entry(run.test_run_id, "github_reported", "already reported; skipping", "github_reporter")
            return {"reported": True, "comment_id": None, "error": None}

        comment = self._build_comment(run)
        last_error: str | None = None
        for attempt in range(self.MAX_RETRIES):
            try:
                comment_id = self._post_comment(run.rt_issue_number, comment)
                audit_entry(run.test_run_id, "github_reported",
                            f"posted comment #{comment_id} to issue #{run.rt_issue_number}",
                            "github_reporter")
                record_github_report(run.test_run_id, run.final_result, comment_id)
                return {"reported": True, "comment_id": comment_id, "error": None}
            except _TransientError as exc:
                last_error = str(exc)
                delay = self.BASE_DELAY * (2 ** attempt)
                audit_entry(run.test_run_id, "github_retry",
                            f"attempt {attempt + 1} failed: {exc}; retrying in {delay}s",
                            "github_reporter")
                time.sleep(delay)
            except Exception as exc:
                last_error = str(exc)
                break

        audit_entry(run.test_run_id, "github_failed", last_error, "github_reporter")
        record_github_report(run.test_run_id, run.final_result, None, last_error)
        return {"reported": False, "comment_id": None, "error": last_error}

    def can_close_issue(self, run: TestRun) -> bool:
        """Whether the rt Issue may be auto-closed for this PASS run.

        Conditions:
        1. final_result is PASS.
        2. The run was successfully reported to GitHub.
        3. The reported version and commit_sha match the run's values.
        4. All mandatory checks were evaluated.
        5. All mandatory checks have status PASS (not WARNING/SKIPPED/etc).
        6. The run has not already been used for acceptance.
        """
        if run.final_result != "PASS":
            return False
        if run.rt_issue_number is None:
            return False
        reported = run.github_report
        if not reported or not reported.get("reported_at"):
            return False
        if reported.get("already_accepted"):
            return False
        expected_version = run.version
        expected_sha = run.commit_sha
        reported_version = reported.get("version")
        reported_sha = reported.get("commit_sha")
        if expected_version and reported_version and expected_version != reported_version:
            return False
        if expected_sha and reported_sha and expected_sha != reported_sha:
            return False
        mandatory_checks = [c for c in run.checks if c.mandatory]
        if not mandatory_checks:
            return False
        non_pass_mandatory = [c for c in mandatory_checks if c.status != CheckStatus.PASS]
        if non_pass_mandatory:
            return False
        return True

    def close_issue(self, run: TestRun) -> dict[str, Any]:
        """Close the rt Issue after a verified PASS. Returns status dict."""
        if not self.can_close_issue(run):
            return {"closed": False, "error": "PASS conditions not met for closing"}
        repo = os.getenv("GITHUB_REPOSITORY", "")
        if not repo:
            return {"closed": False, "error": "GITHUB_REPOSITORY not configured"}
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                _gh(repo, "issue", "close", str(run.rt_issue_number), "--reason", "completed")
                audit_entry(run.test_run_id, "github_closed",
                            f"closed issue #{run.rt_issue_number} after PASS", "github_reporter")
                record_github_report(run.test_run_id, run.final_result, None, None)
                if run.github_report:
                    run.github_report["already_accepted"] = True
                    update_test_run(run)
                return {"closed": True, "error": None}
            except _TransientError as exc:
                last_error = str(exc)
                delay = self.BASE_DELAY * (2 ** attempt)
                audit_entry(run.test_run_id, "github_close_retry",
                            f"attempt {attempt + 1} failed: {exc}; retrying in {delay}s",
                            "github_reporter")
                time.sleep(delay)
            except Exception as exc:
                last_error = str(exc)
                break
        audit_entry(run.test_run_id, "github_close_failed", last_error, "github_reporter")
        return {"closed": False, "error": last_error or "failed to close GitHub issue after retries"}

    def retry_pending(self, run: TestRun) -> dict[str, Any]:
        """Retry GitHub reporting for a run stuck in REPORT_PENDING."""
        return self.report(run)

    def _already_reported(self, test_run_id: str, issue_number: int) -> bool:
        repo = os.getenv("GITHUB_REPOSITORY", "")
        if not repo:
            return False
        try:
            comments = _gh_json(repo, "issue", "view", str(issue_number), "--json", "comments")
            if not comments or "comments" not in comments:
                return False
            pattern = _IDEMPOTENCY_RE
            for comment in comments["comments"]:
                body = comment.get("body", "") or ""
                if pattern.search(body) and test_run_id in body:
                    return True
        except Exception:
            return False
        return False

    def _post_comment(self, issue_number: int, body: str, repo: str | None = None) -> str:
        """Post a comment using the explicit ``gh --repo`` invocation.

        The ``repo`` argument is required so that the command does not fall
        back on ambient credentials when the secret is mis‑configured or
        unavailable.
        """
        if repo is None:
            repo = os.getenv("GITHUB_REPOSITORY", "")
        if not repo:
            raise RuntimeError(
                "GitHub repository not configured; set GITHUB_REPOSITORY environment variable"
            )
        result = _gh(repo, "issue", "comment", str(issue_number), "--body", body)
        if result:
            match = re.search(r"/issues/(\d+)#issuecomment-(\d+)", result)
            return match.group(2) if match else "unknown"
        raise RuntimeError(f"gh comment failed for issue #{issue_number}")

    def _is_transient(self, stderr: str) -> bool:
        lower = stderr.lower()
        return any(marker in lower for marker in
                   ("rate limit", "timeout", "server error", "connection",
                    "502", "503", "504", "network", "temporary"))

    def _build_comment(self, run: TestRun) -> str:
        from .runner import _redact_secrets
        checks_lines = "\n".join(
            f"- {c.name}: {c.status.value}" for c in run.checks
        )
        safe_env = {k: _redact_secrets(str(v)) for k, v in (run.environment or {}).items()}
        return (
            f"REAL TEST RESULT: {run.final_result}\n\n"
            f"{_REPORT_MARKER}{run.test_run_id}\n"
            f"rt: {run.rt_id}\n"
            f"GitHub Issue: #{run.rt_issue_number}\n"
            f"Test Run: {run.test_run_id}\n"
            f"Vertep: {run.version}\n"
            f"Commit: {run.commit_sha}\n"
            f"Environment: {json.dumps(safe_env, ensure_ascii=False)}\n"
            f"CORE: {run.core_node_id or 'unknown'}\n"
            f"Targets: {', '.join(run.targets) if run.targets else 'none'}\n\n"
            f"Checks:\n{checks_lines}\n\n"
            f"Started: {run.started_at}\n"
            f"Finished: {run.finished_at or 'in progress'}\n"
            f"Result: {run.final_result}"
        )


class _TransientError(Exception):
    pass