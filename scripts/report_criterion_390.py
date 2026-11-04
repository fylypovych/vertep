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
        f"{HOST}::test_a_failed_standard_update_is_rolled_back_and_the_retry_succeeds",
        f"{HOST}::test_offline_target_rejected_without_mutation",
        f"{HOST}::test_core_drives_a_canary_rollout_and_holds_it_for_promotion",
        f"{HOST}::test_a_rollout_to_an_offline_node_fails_instead_of_hanging",
        f"{HOST}::test_rollback_restores_previous_version_and_ready_state",
        f"{HOST}::test_workers_first_rollout_times_out_on_core_rolls_the_worker_back_and_a_retry_succeeds",
    ],
    "F2": [
        "tests/test_core_rollout_coordinator.py::test_postgres_epoch_takeover_stops_active_host_apply",
        f"{HOST}::test_cancel_during_an_active_standard_apply_kills_it_without_a_delayed_effect",
        f"{HOST}::test_losing_the_lease_database_kills_the_active_apply_and_a_retry_succeeds_after_restart",
        f"{HOST}::test_a_recovery_started_during_an_active_apply_does_not_overlap_it",
        "tests/test_core_rollout_coordinator.py::"
        "test_a_write_failure_while_storing_the_package_is_journaled_and_a_retry_succeeds",
        "tests/test_core_rollout_coordinator.py::"
        "test_an_fsync_fault_stops_the_update_with_a_journaled_outcome_and_a_retry_succeeds",
        "tests/test_core_rollout_coordinator.py::"
        "test_validly_signed_but_untrusted_release_is_refused_before_any_host_mutation[compatibility]",
        f"{HOST}::test_recovery_does_not_overlap_with_previous_apply",
        f"{CORE}::test_standard_consumer_updates_core_with_migrations_and_backfills",
        f"{CORE}::test_a_failing_migration_rolls_core_back_with_the_database_restored",
    ],
    "F3": [
        f"{HOST}::test_telegram_notification_correlated_with_standard_apply",
        f"{HOST}::test_telegram_operation_reports_phases_error_and_retry_across_process_restarts",
        "tests/test_update_standard_browser_e2e.py::"
        "test_update_browser_drives_the_standard_executor_with_rollback_and_retry",
    ],
    "P1": [
        f"{HOST}::test_interruption_during_apply_does_not_overwrite_newer_data",
        f"{HOST}::test_retry_does_not_return_deleted_seeds",
        "tests/test_backup_process_acceptance.py::"
        "test_core_reads_back_a_restored_workflow_after_a_backup_service_restore",
        f"{CORE}::test_core_reads_back_its_jobs_and_approvals_across_update_rollback_and_recreate",
    ],
    "P3": [
        "tests/test_backup_process_acceptance.py::test_core_caller_surfaces_restore_refusals_without_mutation",
        "tests/test_backup_process_acceptance.py::"
        "test_telegram_restore_caller_reports_refusals_and_retry_through_core",
        "tests/test_backup_process_acceptance.py::"
        "test_connected_healthy_restore_returns_normal_after_core_restart",
        "tests/test_backup_process_acceptance.py::"
        "test_telegram_restore_completes_with_a_terminal_success_on_healthy_dependencies",
        "tests/test_backup_restore_browser_e2e.py::"
        "test_browser_restore_runs_through_real_core_and_backup_service",
    ],
    "P4": [
        f"{HOST}::test_native_restore_is_read_back_by_the_consumer_across_update_restart_and_rollback",
        f"{HOST}::test_application_consumer_reads_back_versions_and_approvals",
        f"{CORE}::test_core_reads_back_a_native_render_across_update_rollback_and_recreate",
        f"{CORE}::test_core_reads_back_a_pinned_mpt_output_across_update_and_rollback",
        *(f"tests/test_mpt_process_lifecycle.py::test_the_application_reads_back_versions_"
          f"approvals_receipts_hashes_and_mappings_across_recreate_and_restore[{engine}]"
          for engine in ("native", "money-printer")),
        f"{CORE}::test_core_reads_back_its_jobs_and_approvals_across_update_rollback_and_recreate",
    ],
}

# #401 P5: source subcases with no executed test yet. A criterion with any of them open is
# never PASS, whatever the listed tests did (absent coverage is not a pass).
OPEN_SUBCASES = {
}

# #393 audit: green on its own, but not proof of the row (reason recorded in the report).
NOT_PROOF = {
    f"{HOST}::test_recovery_does_not_overlap_with_previous_apply":
        "starts from a signature refusal before apply; no active apply to overlap",
    f"{HOST}::test_application_consumer_reads_back_versions_and_approvals":
        "reads files via docker exec sha256sum, not the application reader",
    f"{HOST}::test_offline_target_rejected_without_mutation":
        "requests a version that is not published; no offline node is involved",
}


def junit_results(paths: list[Path], sources: dict[str, str] | None = None) -> dict[str, str]:
    """Exact test ID → PASS/FAIL/SKIPPED from legacy JUnit (classname tests.module).

    ``sources`` (when given) records the JUnit file each counted result came from."""
    results: dict[str, str] = {}
    sources = {} if sources is None else sources
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            test_id = f"{case.get('classname', '').replace('.', '/')}.py::{case.get('name', '')}"
            if case.find("failure") is not None or case.find("error") is not None:
                results[test_id] = "FAIL"
            elif case.find("skipped") is not None:
                if results.setdefault(test_id, "SKIPPED") != "SKIPPED":
                    continue
            else:
                results[test_id] = "PASS"
            sources[test_id] = str(path)
    return results


def report(paths: list[Path], run_sha: str | None = None) -> dict:
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True).stdout.strip()
    # #396 P5: JUnit is counted only for the exact SHA it was produced from.
    foreign = bool(run_sha) and run_sha != sha
    sources: dict[str, str] = {}
    results = {} if foreign else junit_results(paths, sources)
    run_url = (f"{os.getenv('GITHUB_SERVER_URL')}/{os.getenv('GITHUB_REPOSITORY')}/actions/runs/"
               f"{os.getenv('GITHUB_RUN_ID')}" if os.getenv("GITHUB_RUN_ID") else None)
    criteria = {}
    for criterion, test_ids in CRITERIA.items():
        rows = []
        for test_id in test_ids:
            actual = results.get(test_id, "NOT_RUN")
            actual = "NOT_RUN" if actual == "SKIPPED" else actual
            rows.append({"test_id": test_id, "expected": "PASS", "actual": actual,
                         "junit": sources.get(test_id),
                         "proof": test_id not in NOT_PROOF,
                         **({"not_proof_reason": NOT_PROOF[test_id]} if test_id in NOT_PROOF else {})})
        actuals = {row["actual"] for row in rows}
        # A row recorded as not proof stays visible with its reason but neither proves nor
        # blocks the criterion: only the proof rows decide PASS (a FAIL anywhere still fails).
        proof = {row["actual"] for row in rows if row["proof"]}
        open_subcases = OPEN_SUBCASES.get(criterion, [])
        if "FAIL" in actuals:
            overall = "FAIL"
        elif proof == {"PASS"} and not open_subcases:
            overall = "PASS"
        elif "PASS" in actuals:
            overall = "PARTIAL"
        else:
            overall = "NOT_RUN"
        criteria[criterion] = {"overall": overall, "tests": rows, "open_subcases": open_subcases}
    return {"issue": "i.0.0.3.96", "harness_sha": sha, "run_sha": run_sha or sha,
            "run_url": run_url,
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
