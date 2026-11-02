#!/usr/bin/env python3
"""Criterion-level report for the #390 remainder (#393: F1-F3, P1, P3, P4).

Every row status is read from a JUnit file of the run that executed it; nothing is
hardcoded.  A test that is absent or skipped in the JUnit is NOT_RUN, a failed one FAIL.
Tests that the #393 audit showed do not prove their row are listed in ``NOT_PROOF``:
they still report their own result, but the criterion can never be PASS on them.

    python scripts/report_criterion_390.py --junit host-consumer.xml [--junit ...]
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "tests/test_standard_host_consumer_docker.py"
CORE = "tests/test_standard_core_consumer_docker.py"

CRITERIA = {
    "F1": [
        f"{HOST}::test_standard_consumer_moves_a_running_vertep_worker_and_core_observes_it",
        f"{HOST}::test_standard_consumer_drains_and_admits_workers_during_update",
        f"{HOST}::test_update_timeout_and_retry_with_standard_consumer",
        f"{HOST}::test_offline_target_rejected_without_mutation",
        f"{HOST}::test_canary_promotion_flow_with_standard_consumer",
        f"{HOST}::test_rollback_restores_previous_version_and_ready_state",
    ],
    "F2": [
        f"{HOST}::test_cancel_during_an_active_standard_apply_kills_it_without_a_delayed_effect",
        f"{HOST}::test_recovery_does_not_overlap_with_previous_apply",
        f"{CORE}::test_standard_consumer_updates_core_with_migrations_and_backfills",
        f"{CORE}::test_a_failing_migration_rolls_core_back_with_the_database_restored",
    ],
    "F3": [
        f"{HOST}::test_telegram_notification_correlated_with_standard_apply",
    ],
    "P1": [
        f"{HOST}::test_interruption_during_apply_does_not_overwrite_newer_data",
        f"{HOST}::test_retry_does_not_return_deleted_seeds",
    ],
    "P3": [
        "tests/test_backup_process_acceptance.py::test_core_caller_surfaces_restore_refusals_without_mutation",
        "tests/test_backup_process_acceptance.py::"
        "test_telegram_restore_caller_reports_refusals_and_retry_through_core",
    ],
    "P4": [
        f"{HOST}::test_native_restore_is_read_back_by_the_consumer_across_update_restart_and_rollback",
        f"{HOST}::test_application_consumer_reads_back_versions_and_approvals",
    ],
}

# #393 audit: green on its own, but not proof of the row (reason recorded in the report).
NOT_PROOF = {
    f"{HOST}::test_update_timeout_and_retry_with_standard_consumer":
        "one successful update; no timeout or retry is exercised",
    f"{HOST}::test_canary_promotion_flow_with_standard_consumer":
        "waits for the previous version after update; promotion not proven",
    f"{HOST}::test_recovery_does_not_overlap_with_previous_apply":
        "starts from a signature refusal before apply; no active apply to overlap",
    f"{HOST}::test_application_consumer_reads_back_versions_and_approvals":
        "reads files via docker exec sha256sum, not the application reader",
    f"{HOST}::test_offline_target_rejected_without_mutation":
        "requests a version that is not published; no offline node is involved",
}


def junit_results(paths: list[Path]) -> dict[str, str]:
    """Exact test ID → PASS/FAIL/SKIPPED from legacy JUnit (classname tests.module)."""
    results: dict[str, str] = {}
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            test_id = f"{case.get('classname', '').replace('.', '/')}.py::{case.get('name', '')}"
            if case.find("failure") is not None or case.find("error") is not None:
                results[test_id] = "FAIL"
            elif case.find("skipped") is not None:
                results.setdefault(test_id, "SKIPPED")
            else:
                results[test_id] = "PASS"
    return results


def report(paths: list[Path], run_sha: str | None = None) -> dict:
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True).stdout.strip()
    # #396 P5: JUnit is counted only for the exact SHA it was produced from.
    foreign = bool(run_sha) and run_sha != sha
    results = {} if foreign else junit_results(paths)
    criteria = {}
    for criterion, test_ids in CRITERIA.items():
        rows = []
        for test_id in test_ids:
            actual = results.get(test_id, "NOT_RUN")
            actual = "NOT_RUN" if actual == "SKIPPED" else actual
            rows.append({"test_id": test_id, "actual": actual,
                         "proof": test_id not in NOT_PROOF,
                         **({"not_proof_reason": NOT_PROOF[test_id]} if test_id in NOT_PROOF else {})})
        actuals = {row["actual"] for row in rows}
        if "FAIL" in actuals:
            overall = "FAIL"
        elif actuals == {"PASS"} and all(row["proof"] for row in rows):
            overall = "PASS"
        elif "PASS" in actuals:
            overall = "PARTIAL"
        else:
            overall = "NOT_RUN"
        criteria[criterion] = {"overall": overall, "tests": rows}
    return {"issue": "i.0.0.3.96", "harness_sha": sha, "run_sha": run_sha or sha,
            **({"error": f"JUnit run SHA {run_sha} is not the checked-out {sha}"} if foreign else {}),
            "junit_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in paths if path.is_file()},
            "environment": {"system": platform.platform(), "machine": platform.machine()},
            "junit": [str(path) for path in paths], "criteria": criteria,
            "summary": {status: sum(1 for value in criteria.values() if value["overall"] == status)
                        for status in ("PASS", "PARTIAL", "FAIL", "NOT_RUN")}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "criterion-390.json")
    parser.add_argument("--run-sha", default=os.getenv("GITHUB_SHA"),
                        help="commit the JUnit was produced from (defaults to GITHUB_SHA)")
    arguments = parser.parse_args()
    value = report(arguments.junit, arguments.run_sha)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(value["summary"]))


if __name__ == "__main__":
    main()
