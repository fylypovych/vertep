#!/usr/bin/env python3
"""Release gate: verify CI and Browser E2E passed for the exact release SHA.

Checks the GitHub Actions API for the workflow runs associated with the
commit SHA that triggered the release workflow.  Both the ``CI`` workflow
and the ``Browser E2E`` workflow must have at least one **completed** run
with ``conclusion == "success"`` for the *same* SHA.  A successful run for a
different SHA does **not** satisfy the gate.

Negative checks (any of these blocks the release):
- no run found for the workflow (missing)
- run still pending / in progress after the wait budget is exhausted
- run cancelled or skipped
- run failed
- run succeeded but for a different SHA (rejected explicitly)

With ``--wait-timeout`` the gate polls the API until every required workflow
reaches a terminal conclusion instead of failing on the first pending sample.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


REQUIRED_WORKFLOWS = {
    "CI",
    "Browser E2E",
}

# Conclusions that explicitly do NOT satisfy the gate.
BLOCKING_CONCLUSIONS = {
    "failure": "failed",
    "cancelled": "cancelled",
    "skipped": "skipped",
    "neutral": "neutral",
}

# Statuses that are not yet terminal — the gate must wait, not pass.
NON_TERMINAL_STATUSES = {"queued", "in_progress", "pending", "waiting"}


def _gh_api(path: str) -> str:
    """Invoke ``gh api`` and return the raw response text."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    cmd = ["gh", "api", f"repos/{path}", "--jq", "."]
    if token:
        cmd.extend(["--header", f"Authorization: Bearer {token}"])
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"gh api failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def _workflow_runs_for_sha(sha: str, repo: str) -> list[dict]:
    """Return all workflow runs for *sha* as a list of dicts."""
    raw = _gh_api(f"{repo}/actions/runs?head_sha={sha}")
    data = json.loads(raw)
    return data.get("workflow_runs", [])


def _conclusion_for_workflow(runs: list[dict], workflow_name: str) -> tuple[str, str, str]:
    """Return (status, conclusion, head_sha) for the most recent run of *workflow_name*."""
    matching = [r for r in runs if r.get("name") == workflow_name]
    if not matching:
        return ("not found", "", "")
    run = matching[0]
    return (run.get("status", ""), run.get("conclusion", ""), run.get("head_sha", ""))


def _evaluate(status: str, conclusion: str, run_sha: str, expected_sha: str) -> tuple[bool, str]:
    """Return (passed, reason) for a single workflow check.

    Only a **completed** run with **success** conclusion whose head_sha
    matches *expected_sha* passes.  Missing, pending, cancelled, skipped,
    failed and wrong-SHA runs are all explicit failures.
    """
    if status == "not found":
        return False, "missing: no workflow run found for this SHA"
    if status in NON_TERMINAL_STATUSES:
        return False, f"{status}: workflow run has not terminated yet"
    if conclusion == "success":
        if run_sha != expected_sha:
            return False, (
                f"wrong SHA: run succeeded on {run_sha[:12]}, "
                f"expected {expected_sha[:12]}"
            )
        return True, "ok"
    reason = BLOCKING_CONCLUSIONS.get(conclusion, conclusion or "unknown")
    return False, f"{reason}: workflow concluded '{conclusion}'"


def check_gate(sha: str, repo: str) -> dict:
    """Run the release gate check and return a report dict."""
    runs = _workflow_runs_for_sha(sha, repo)
    checks: list[dict] = []
    all_passed = True

    for wf_name in sorted(REQUIRED_WORKFLOWS):
        status, conclusion, run_sha = _conclusion_for_workflow(runs, wf_name)
        passed, reason = _evaluate(status, conclusion, run_sha, sha)
        all_passed = all_passed and passed
        checks.append({
            "workflow": wf_name,
            "status": status,
            "conclusion": conclusion,
            "run_sha": run_sha,
            "passed": passed,
            "reason": reason,
        })

    return {
        "sha": sha,
        "repo": repo,
        "passed": all_passed,
        "checks": checks,
    }


def _is_waitable(report: dict) -> bool:
    """True while the gate can still change by waiting.

    A missing run stays waitable as well: workflow runs are registered by the
    API asynchronously, so an immediate call right after the push legitimately
    observes nothing yet.
    """
    return any(
        check["status"] == "not found" or check["status"] in NON_TERMINAL_STATUSES
        for check in report["checks"]
    )


def wait_for_gate(
    sha: str,
    repo: str,
    *,
    timeout: float,
    interval: float = 15.0,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> dict:
    """Poll the gate until it passes, fails or the timeout budget is spent."""
    deadline = monotonic() + timeout
    report = check_gate(sha, repo)
    while not report["passed"] and _is_waitable(report):
        if monotonic() >= deadline:
            break
        sleep(min(interval, max(0.0, deadline - monotonic())))
        report = check_gate(sha, repo)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify CI and Browser E2E gates passed for the release SHA."
    )
    parser.add_argument("sha", help="Commit SHA to verify")
    parser.add_argument(
        "--repo", default=None,
        help="Repository in OWNER/NAME format (defaults to $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--wait-timeout", type=float, default=0.0,
        help="seconds to wait for pending runs before failing the gate",
    )
    parser.add_argument(
        "--wait-interval", type=float, default=15.0,
        help="seconds between gate samples while waiting",
    )
    args = parser.parse_args()

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("Error: --repo or GITHUB_REPOSITORY must be set", file=sys.stderr)
        sys.exit(2)

    if args.wait_timeout > 0:
        report = wait_for_gate(
            args.sha, repo, timeout=args.wait_timeout, interval=args.wait_interval
        )
    else:
        report = check_gate(args.sha, repo)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
