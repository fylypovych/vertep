"""GitHub integration for Real Test reporting.

Posts idempotent comments to the GitHub ``rt`` Issue that corresponds to
a completed test run.  Uses the GitHub REST API via ``urllib`` — no
external CLI dependencies.  Credentials are supplied via the encrypted
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
import time
import urllib.error
import urllib.request
from typing import Any

from ..first_run import integration_secret_status
from .models import CheckStatus, TestRun
from .storage import audit_entry, record_github_report, update_test_run

_REPORT_MARKER = "REAL-TEST-RUN:"
_IDEMPOTENCY_RE = re.compile(rf"{re.escape(_REPORT_MARKER)}\s*([0-9a-f]{{32}})")
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _deployment_sha() -> str:
    """Independently resolve this deployment's commit SHA (no VERSION/unknown)."""
    github_sha = os.getenv("GITHUB_SHA", "").strip()
    if github_sha:
        return github_sha
    try:
        import subprocess
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


def _deployment_version() -> str:
    """Independently resolve the installed Vertep version for acceptance."""
    from ..version import application_version
    return application_version().strip()


def _expected_mandatory_names(rt_id: str) -> set[str]:
    """The scenario's mandatory check set — full coverage, not a single PASS."""
    from .scenarios import find_scenario
    scenario = find_scenario(rt_id=rt_id)
    if not scenario:
        return set()
    return set(scenario.get("checks") or [])


def _is_configured() -> bool:
    return integration_secret_status().get("github_pat", False)


def _get_token() -> str:
    """Read the GitHub PAT from the encrypted secret store.

    Raises RuntimeError if unavailable — never falls back on ambient
    credentials.
    """
    if not integration_secret_status().get("github_pat", False):
        raise RuntimeError("github_pat is not configured")
    from ..first_run import _read_encrypted_secrets
    store = _read_encrypted_secrets()
    token = store.get("github_pat", "")
    if not token:
        raise RuntimeError("github_pat secret is empty in the store")
    return token


def _repo() -> str:
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not repo:
        raise RuntimeError(
            "GitHub repository not configured; set GITHUB_REPOSITORY environment variable"
        )
    return repo


def _api_base() -> str:
    """Base URL for the GitHub REST API (overridable for controlled tests)."""
    return os.getenv("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")


def _api_request(
    method: str,
    url: str,
    body: dict | None = None,
    timeout: int = 30,
) -> dict:
    """Make an authenticated GitHub API request.

    Returns parsed JSON dict.  Raises ``_TransientError`` on transient
    failures and ``RuntimeError`` on permanent failures.
    """
    token = _get_token()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        stderr = ""
        try:
            stderr = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        code = exc.code or 0
        if code == 403 and "rate limit" in stderr.lower():
            raise _TransientError(f"GitHub API rate limit: {stderr}")
        if code in (502, 503, 504):
            raise _TransientError(f"GitHub API transient error {code}: {stderr}")
        if code in (408, 429):
            raise _TransientError(f"GitHub API throttled/unavailable {code}: {stderr}")
        raise RuntimeError(f"GitHub API error {code}: {stderr}")
    except urllib.error.URLError as exc:
        raise _TransientError(f"GitHub API network error: {exc.reason}")
    except TimeoutError:
        raise _TransientError("GitHub API request timed out")
    except (ConnectionError, OSError) as exc:
        raise _TransientError(f"GitHub API connection failed: {exc}")


def _post_comment(issue_number: int, body: str) -> str:
    """Post a comment to a GitHub issue. Returns the comment ID."""
    repo = _repo()
    url = f"{_api_base()}/repos/{repo}/issues/{issue_number}/comments"
    result = _api_request("POST", url, body={"body": body})
    comment_id = str(result.get("id", "unknown"))
    return comment_id


def _get_comments(issue_number: int) -> list[dict]:
    """Fetch all comments for a GitHub issue, paginating until exhausted.

    GitHub caps per_page at 100 and may return a Link header for the next
    page; a single request can silently miss a marker that lives beyond
    page 1, breaking idempotency checks.
    """
    repo = _repo()
    all_comments: list[dict] = []
    page = 1
    while True:
        url = (f"{_api_base()}/repos/{repo}/issues/{issue_number}/comments"
               f"?per_page=100&page={page}")
        response = _api_request("GET", url)
        if not isinstance(response, list) or not response:
            break
        all_comments.extend(response)
        # GitHub returns fewer than per_page on the last page.
        if len(response) < 100:
            break
        page += 1
        # Safety cap to avoid runaway loops if the API misbehaves.
        if page > 10:
            break
    return all_comments


def _close_issue(issue_number: int) -> None:
    """Close a GitHub issue with 'completed' reason."""
    repo = _repo()
    url = f"{_api_base()}/repos/{repo}/issues/{issue_number}"
    _api_request("PATCH", url, body={"state": "closed", "state_reason": "completed"})


class GitHubReporter:
    """Publishes test-run results to the corresponding GitHub ``rt`` Issue."""

    MAX_RETRIES = 3
    BASE_DELAY = 2.0

    def report(self, run: TestRun) -> dict[str, Any]:
        """Post the test report comment to the rt Issue.

        Returns ``{"reported": bool, "comment_id": str | None, "error": str | None}``.
        On persistent failure the error is stored locally for later retry.
        Idempotent: a ``test_run_id`` is reported only once; re-running the
        report for an already-reported run is a no-op.
        """
        if run.rt_issue_number is None:
            return {"reported": False, "comment_id": None,
                    "error": "No rt_issue_number configured for this scenario"}

        if not _is_configured():
            return {"reported": False, "comment_id": None,
                    "error": "github_pat is not configured"}

        try:
            if self._already_reported(run.test_run_id, run.rt_issue_number):
                audit_entry(run.test_run_id, "github_reported", "already reported; skipping", "github_reporter")
                return {"reported": True, "comment_id": None, "error": None}
        except _TransientError:
            # Outage during pre-check: fail closed into REPORT_PENDING via the
            # normal retry path — never mask the outage as success.
            audit_entry(run.test_run_id, "github_precheck_unavailable",
                        "marker lookup unavailable; proceeding to POST attempt",
                        "github_reporter")

        comment = self._build_comment(run)
        last_error: str | None = None
        for attempt in range(self.MAX_RETRIES):
            try:
                comment_id = _post_comment(run.rt_issue_number, comment)
                audit_entry(run.test_run_id, "github_reported",
                            f"posted comment #{comment_id} to issue #{run.rt_issue_number}",
                            "github_reporter")
                record_github_report(run.test_run_id, run.final_result, comment_id,
                                     version=run.version, commit_sha=run.commit_sha)
                return {"reported": True, "comment_id": comment_id, "error": None}
            except _TransientError as exc:
                # Unknown POST outcome: the comment may have been created
                # server-side despite the transport error (timeout/reset).
                # Reconcile via marker lookup before blind retry — otherwise
                # a retry would double-post (i.0.0.0.96 exactly-once).
                try:
                    if self._already_reported(run.test_run_id, run.rt_issue_number):
                        audit_entry(run.test_run_id, "github_idempotent_skip",
                                    "unknown POST outcome reconciled — marker present, skipping retry",
                                    "github_reporter")
                        return {"reported": True, "comment_id": None, "error": None}
                except _TransientError:
                    pass  # lookup itself transient — proceed to normal retry
                last_error = str(exc)
                delay = self.BASE_DELAY * (2 ** attempt)
                audit_entry(run.test_run_id, "github_retry",
                            f"attempt {attempt + 1} failed: {exc}; retrying in {delay}s",
                            "github_reporter")
                time.sleep(delay)
            except Exception as exc:
                # Permanent POST failure: reconcile before giving up — a
                # concurrent reporter may have posted the marker meanwhile.
                try:
                    if self._already_reported(run.test_run_id, run.rt_issue_number):
                        audit_entry(run.test_run_id, "github_idempotent_skip",
                                    "concurrent post detected — marker already present, skipping",
                                    "github_reporter")
                        return {"reported": True, "comment_id": None, "error": None}
                except _TransientError:
                    pass
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
        3. The run carries a resolved deployment identity (full commit SHA and
           version); ``unknown``/empty values are rejected (Issue #95).
        4. The reported version and commit_sha are present and *exactly* match
           the run's values — a missing reported identity is never accepted.
        5. The run's identity matches the independently resolved expected
           identity of this deployment (env/git SHA and installed version),
           so a stale or forged report cannot close the Issue.
        6. All mandatory checks were evaluated and cover the scenario's full
           mandatory check set.
        7. All mandatory checks have status PASS (not WARNING/SKIPPED/etc).
        8. The run has not already been used for acceptance.
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
        expected_version = (run.version or "").strip()
        expected_sha = (run.commit_sha or "").strip()
        if not expected_version or expected_version.lower() in {"unknown", "none"}:
            return False
        if not _FULL_SHA_RE.fullmatch(expected_sha):
            return False
        reported_version = (reported.get("version") or "").strip()
        reported_sha = (reported.get("commit_sha") or "").strip()
        if not reported_version or not reported_sha:
            return False
        if expected_version != reported_version or expected_sha != reported_sha:
            return False
        # Independent identity: resolve the deployment's own SHA/version now and
        # compare — never trust the report alone (Issue #95, #66).
        if expected_sha != _deployment_sha() or expected_version != _deployment_version():
            return False
        mandatory_checks = [c for c in run.checks if c.mandatory]
        if not mandatory_checks:
            return False
        non_pass_mandatory = [c for c in mandatory_checks if c.status != CheckStatus.PASS]
        if non_pass_mandatory:
            return False
        expected_names = _expected_mandatory_names(run.rt_id)
        reported_names = {c.name for c in mandatory_checks}
        if expected_names and not expected_names.issubset(reported_names):
            return False
        return True

    def close_issue(self, run: TestRun) -> dict[str, Any]:
        """Close the rt Issue after a verified PASS. Returns status dict."""
        if not self.can_close_issue(run):
            return {"closed": False, "error": "PASS conditions not met for closing"}
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                _close_issue(run.rt_issue_number)
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
        """Check if this test_run_id was already reported to the issue.

        Does NOT swallow lookup errors — returns False on transient
        failures so the caller can retry rather than silently skipping.
        Always fetches ALL pages (including beyond per_page=100) to
        detect markers placed on any page.
        """
        try:
            comments = _get_comments(issue_number)
            pattern = _IDEMPOTENCY_RE
            for comment in comments:
                body = comment.get("body", "") or ""
                if pattern.search(body) and test_run_id in body:
                    return True
            return False
        except _TransientError:
            raise
        except Exception:
            # Return False so caller can retry; do not silently skip
            return False

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
