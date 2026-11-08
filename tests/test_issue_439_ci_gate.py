"""#439 (#413 AC9): every split fleet job can only pass the release gate by passing itself.

The release gate accepts a SHA only when the whole ``CI`` workflow concluded success
(``scripts/check-release-gate.py``, covered by ``tests/test_release.py``). A job lowers
that conclusion unless it is skipped by a condition or allowed to fail, so neither may
exist for the fleet jobs, and each matrix part must be one the criterion report reads.
"""
from pathlib import Path

import yaml

CI = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
JOBS = CI["jobs"]
FLEET = [name for name in JOBS if name.startswith("fleet-")]


def test_the_split_fleet_jobs_exist():
    assert set(FLEET) == {"fleet-persistence-runtime-acceptance", "fleet-rollout-acceptance",
                          "fleet-cancel-path-acceptance"}


def test_a_fleet_job_can_neither_be_skipped_nor_fail_quietly():
    for name in FLEET:
        job = JOBS[name]
        assert "if" not in job, f"{name} could be skipped and still leave CI green"
        assert not job.get("continue-on-error"), name
        assert job.get("strategy", {}).get("fail-fast", False) is False, name
        for step in job["steps"]:
            if "pytest" in str(step.get("run", "")):
                assert not step.get("continue-on-error"), f"{name}: a failing test step must fail the job"
                assert "|| true" not in step["run"], name


def test_every_rollout_part_runs_and_reports_its_own_tests():
    job = JOBS["fleet-rollout-acceptance"]
    parts = job["strategy"]["matrix"]["include"]
    tests = [test for part in parts for test in part["tests"].split()]
    assert len(tests) == len(set(tests)), "a rollout test runs in two parts"
    assert {part["part"] for part in parts} == {"workers-first", "consumer"}
    run = next(step["run"] for step in job["steps"] if "pytest" in str(step.get("run", "")))
    assert "${{ matrix.tests }}" in run
    upload = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["with"]["name"] == "fleet-rollout-${{ matrix.part }}-${{ github.sha }}"
    assert upload["with"]["if-no-files-found"] == "error"
    report = JOBS["criterion-390-report"]
    pattern = next(step["with"]["pattern"] for step in report["steps"]
                   if step.get("uses", "").startswith("actions/download-artifact"))
    for part in parts:
        assert f"fleet-rollout-{part['part']}" in pattern
    assert set(FLEET) <= set(report["needs"])
