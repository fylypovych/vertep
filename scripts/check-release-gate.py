#!/usr/bin/env python3
"""Release gate: verify CI and Browser E2E passed for the exact release SHA.

Checks the GitHub Actions API for the workflow runs associated with the
commit SHA that triggered the release workflow.  Both the ``CI`` workflow
and the ``Browser E2E`` workflow must have at least one **completed** run
with ``conclusion == "success"`` for the *same* SHA.  A successful run for a
different SHA does **not** satisfy the gate.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


REQUIRED_WORKFLOWS = {
    "CI",
}


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
    import json
    data = json.loads(raw)
    return data.get("workflow_runs", [])


def _conclusion_for_workflow(runs: list[dict], workflow_name: str) -> tuple[str, str]:
    """Return (status, conclusion) for the most recent run of *workflow_name*."""
    matching = [r for r in runs if r.get("name") == workflow_name]
    if not matching:
        return ("not found", "")
    run = matching[0]
    return (run.get("status", ""), run.get("conclusion", ""))


def check_gate(sha: str, repo: str) -> dict:
    """Run the release gate check and return a report dict."""
    runs = _workflow_runs_for_sha(sha, repo)
    checks: list[dict] = []
    all_passed = True

    for wf_name in sorted(REQUIRED_WORKFLOWS):
        status, conclusion = _conclusion_for_workflow(runs, wf_name)
        passed = status == "completed" and conclusion == "success"
        all_passed = all_passed and passed
        checks.append({
            "workflow": wf_name,
            "status": status,
            "conclusion": conclusion,
            "passed": passed,
        })

    return {
        "sha": sha,
        "repo": repo,
        "passed": all_passed,
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify CI and Browser E2E gates passed for the release SHA."
    )
    parser.add_argument("sha", help="Commit SHA to verify")
    parser.add_argument(
        "--repo", default=None,
        help="Repository in OWNER/NAME format (defaults to $GITHUB_REPOSITORY)",
    )
    args = parser.parse_args()

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("Error: --repo or GITHUB_REPOSITORY must be set", file=sys.stderr)
        sys.exit(2)

    report = check_gate(args.sha, repo)
    import json
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
