#!/usr/bin/env python3
"""Weekly GitHub cleanup (owner decision 28.11.2026).

* Workflow runs: every run that did not pass (failure, timed_out, startup_failure,
  cancelled) is deleted, except the newest run of each workflow, which always stays
  so the current state remains visible. Runs still in progress are never touched.
* Vertep releases (tags ``A.B.C.D``; component releases ``<component>.A.B.C.D`` are not
  touched): the latest release stays, plus the three newest earlier releases that are
  fully green — CI, Browser E2E and Vertep Release all succeeded for the tag's commit.
  Every other Vertep release is deleted; its Git tag is kept (history is never rewritten).

Release greenness is decided before any run is deleted. Any API error stops the run
before deletions (fail closed). ``--dry-run`` only prints the plan.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
NOT_PASSED = {"failure", "timed_out", "startup_failure", "cancelled"}
GREEN_WORKFLOWS = ("CI", "Browser E2E", "Vertep Release")
KEEP_GREEN_PREVIOUS = 3
VERSION_TAG = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.split("."))


def runs_to_delete(runs: list[dict], current_run_id: int | None = None) -> list[dict]:
    """Not-passed completed runs, except the newest run of each workflow."""
    newest: dict[int, dict] = {}
    for run in runs:
        known = newest.get(run["workflow_id"])
        if known is None or (run["created_at"], run["id"]) > (known["created_at"], known["id"]):
            newest[run["workflow_id"]] = run
    keep = {run["id"] for run in newest.values()}
    return [run for run in runs
            if run["id"] not in keep and run["id"] != current_run_id
            and run.get("status") == "completed" and run.get("conclusion") in NOT_PASSED]


def is_green(runs_for_sha: list[dict]) -> bool:
    """The newest completed run of each required workflow for one commit succeeded."""
    latest: dict[str, dict] = {}
    for run in runs_for_sha:
        if run.get("status") != "completed":
            continue
        known = latest.get(run["name"])
        if known is None or (run["created_at"], run["id"]) > (known["created_at"], known["id"]):
            latest[run["name"]] = run
    return all(latest.get(name, {}).get("conclusion") == "success" for name in GREEN_WORKFLOWS)


def releases_to_delete(releases: list[dict], latest_tag: str, green: dict[str, bool]) -> list[dict]:
    """Every published Vertep release except the latest and the 3 newest green earlier ones."""
    vertep = [release for release in releases
              if VERSION_TAG.match(release["tag_name"]) and not release.get("draft")]
    if latest_tag not in {release["tag_name"] for release in vertep}:
        raise ValueError(f"latest release {latest_tag!r} is not a published Vertep release")
    keep = {latest_tag}
    earlier = sorted((release for release in vertep
                      if version_key(release["tag_name"]) < version_key(latest_tag)),
                     key=lambda release: version_key(release["tag_name"]), reverse=True)
    for release in earlier:
        if len(keep) > KEEP_GREEN_PREVIOUS:
            break
        if green.get(release["tag_name"]):
            keep.add(release["tag_name"])
    return [release for release in vertep if release["tag_name"] not in keep]


class GitHub:
    def __init__(self, repository: str, token: str) -> None:
        self.repository, self.token = repository, token

    def call(self, method: str, path: str) -> object:
        request = urllib.request.Request(f"{API}/repos/{self.repository}{path}", method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        return json.loads(body) if body else None

    def pages(self, path: str, key: str | None = None) -> list[dict]:
        items, page = [], 1
        separator = "&" if "?" in path else "?"
        while True:
            answer = self.call("GET", f"{path}{separator}per_page=100&page={page}")
            chunk = answer[key] if key else answer
            items.extend(chunk)
            if len(chunk) < 100:
                return items
            page += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    github = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    current = int(os.environ["GITHUB_RUN_ID"]) if os.getenv("GITHUB_RUN_ID") else None

    # Plan everything first; an API error here stops before any deletion.
    releases = github.pages("/releases")
    latest_tag = github.call("GET", "/releases/latest")["tag_name"]
    green = {}
    for release in releases:
        tag = release["tag_name"]
        if VERSION_TAG.match(tag) and tag != latest_tag and not release.get("draft"):
            sha = github.call("GET", f"/commits/{tag}")["sha"]
            green[tag] = is_green(github.pages(f"/actions/runs?head_sha={sha}", "workflow_runs"))
    doomed_releases = releases_to_delete(releases, latest_tag, green)
    doomed_runs = runs_to_delete(github.pages("/actions/runs", "workflow_runs"), current)

    kept = sorted({r["tag_name"] for r in releases if VERSION_TAG.match(r["tag_name"])}
                  - {r["tag_name"] for r in doomed_releases}, key=version_key, reverse=True)
    print(f"Latest release: {latest_tag}; kept Vertep releases: {', '.join(kept)}")
    print(f"Releases to delete ({len(doomed_releases)}): {', '.join(r['tag_name'] for r in doomed_releases) or '-'}")
    print(f"Workflow runs to delete ({len(doomed_runs)}):")
    for run in doomed_runs:
        print(f"  {run['id']} {run['name']} {run['conclusion']} {run['created_at']} {run['head_sha'][:8]}")
    if args.dry_run:
        print("Dry run: nothing deleted")
        return 0

    failures = 0
    for release in doomed_releases:
        try:
            github.call("DELETE", f"/releases/{release['id']}")  # the Git tag stays
        except urllib.error.HTTPError as error:
            failures += 1
            print(f"::error::release {release['tag_name']}: HTTP {error.code}", file=sys.stderr)
    for run in doomed_runs:
        try:
            github.call("DELETE", f"/actions/runs/{run['id']}")
        except urllib.error.HTTPError as error:
            failures += 1
            print(f"::error::run {run['id']}: HTTP {error.code}", file=sys.stderr)
    print(f"Deleted {len(doomed_releases)} releases and {len(doomed_runs)} runs, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
