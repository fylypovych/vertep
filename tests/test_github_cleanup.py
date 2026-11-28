"""Weekly GitHub cleanup: failed runs go (newest per workflow stays), four Vertep releases stay."""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("github_cleanup", ROOT / "scripts" / "github-cleanup.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


def run(run_id, workflow, conclusion, created, status="completed", name="CI"):
    return {"id": run_id, "workflow_id": workflow, "name": name, "status": status,
            "conclusion": conclusion, "created_at": created, "head_sha": "a" * 40}


def test_failed_runs_go_except_the_newest_of_each_workflow():
    runs = [run(1, 10, "failure", "2026-11-01"), run(2, 10, "success", "2026-11-02"),
            run(3, 10, "cancelled", "2026-11-03"), run(4, 10, "failure", "2026-11-04"),  # newest CI stays
            run(5, 20, "timed_out", "2026-11-01"), run(6, 20, "success", "2026-11-05"),
            run(7, 20, None, "2026-11-04", status="in_progress"),  # never touched
            run(8, 30, "startup_failure", "2026-11-06"),  # only run of its workflow
            run(9, 20, "failure", "2026-11-03")]
    assert sorted(item["id"] for item in cleanup.runs_to_delete(runs)) == [1, 3, 5, 9]
    assert 9 not in [item["id"] for item in cleanup.runs_to_delete(runs, current_run_id=9)]


def test_green_means_ci_e2e_and_release_all_succeeded_on_their_newest_run():
    ok = [run(1, 1, "success", "1", name="CI"), run(2, 2, "success", "1", name="Browser E2E"),
          run(3, 3, "success", "1", name="Vertep Release")]
    assert cleanup.is_green(ok)
    assert not cleanup.is_green(ok[:2])  # never released
    assert not cleanup.is_green(ok + [run(4, 2, "failure", "2", name="Browser E2E")])
    assert cleanup.is_green(ok + [run(0, 2, "failure", "0", name="Browser E2E")])  # an older failure, rerun green
    assert cleanup.is_green(ok + [run(5, 1, None, "3", status="in_progress", name="CI")])


def release(tag, release_id=None, draft=False):
    return {"tag_name": tag, "id": release_id or tag, "draft": draft}


def test_latest_and_three_newest_green_earlier_releases_stay():
    releases = [release(t) for t in ("0.0.4.66", "0.0.4.65", "0.0.4.64", "0.0.4.63", "0.0.4.62",
                                     "0.0.4.61", "0.0.4.60", "0.0.4.9")]
    releases += [release("brands.0.0.4.17"), release("0.0.4.67", draft=True)]
    green = {"0.0.4.65": True, "0.0.4.64": False, "0.0.4.63": True, "0.0.4.62": False,
             "0.0.4.61": True, "0.0.4.60": True, "0.0.4.9": True}
    doomed = {item["tag_name"] for item in cleanup.releases_to_delete(releases, "0.0.4.66", green)}
    # Kept: latest 0.0.4.66 + green 0.0.4.65, 0.0.4.63, 0.0.4.61. Component releases and drafts untouched.
    assert doomed == {"0.0.4.64", "0.0.4.62", "0.0.4.60", "0.0.4.9"}


def test_versions_sort_numerically_and_the_latest_must_exist():
    releases = [release("0.0.4.100"), release("0.0.4.99"), release("0.0.4.10")]
    green = {"0.0.4.99": True, "0.0.4.10": True}
    assert cleanup.releases_to_delete(releases, "0.0.4.100", green) == []
    with pytest.raises(ValueError):
        cleanup.releases_to_delete(releases, "0.0.5.0", green)


def test_the_workflow_runs_on_monday_midnight_and_dispatch_is_a_dry_run_by_default():
    workflow = yaml.safe_load((ROOT / ".github/workflows/github-cleanup.yml").read_text(encoding="utf-8"))
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert triggers["schedule"] == [{"cron": "0 22 * * 0"}]
    assert triggers["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    assert workflow["permissions"] == {"contents": "write", "actions": "write"}
    script = workflow["jobs"]["cleanup"]["steps"][-1]["run"]
    assert "--dry-run" in script and "scripts/github-cleanup.py" in script
