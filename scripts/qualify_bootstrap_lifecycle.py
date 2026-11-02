#!/usr/bin/env python3
"""Fail-closed disposable bootstrap/update acceptance evidence for Issue #384/#388.

This harness does not call the publishing release workflow.  The update/install
executor trust matrix (tamper, expiry, revoke, channel, downgrade, rotation,
recovery, unsafe archive, symlink, cross-origin, compatibility) runs *inside* a
``linux/<architecture>`` container, and the architecture recorded in the evidence is
the one reported by that test process, not by a separate probe.  The Docker apply
executor needs the host daemon and runs on the host; its architecture is recorded
separately.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TAMPER_EXECUTOR_TEST = ("tests/test_core_rollout_coordinator.py::"
                        "test_tampered_signed_manifest_is_refused_before_any_host_mutation")
EXECUTOR_TARGETS = (
    "tests/test_safe_update.py",
    "tests/test_update_security.py",
    "tests/test_bootstrap_wizard.py",
    "tests/test_release_hardening.py",
    TAMPER_EXECUTOR_TEST,
)
HOST_TARGETS = ("tests/test_apply_deployment_docker.py",)
EXPECTED_MACHINE = {"amd64": "x86_64", "arm64": "aarch64"}
EXECUTOR_IMAGE = "python:3.12-slim"

# Authoritative list of executor-boundary test IDs for B4 criterion-level report.
# Each entry maps a trust dimension to the specific test IDs that prove it.
# Tests run inside the linux/<architecture> container (EXECUTOR_TARGETS) are labelled
# with dimension tags; tests only on the host executor are in HOST_TARGETS.
EXECUTOR_TRUST_DIMENSIONS = {
    # A tampered signed manifest from the HTTPS origin is refused by the update agent
    # before the host consumer runs; release and runtime instance stay unchanged.
    "tamper": [
        TAMPER_EXECUTOR_TEST,
        "tests/test_safe_update.py::test_signed_manifest_and_compatibility",
    ],
    "expiry": [
        "tests/test_safe_update.py::test_manifest_keyring_channel_and_validity",
    ],
    "revoke": [
        "tests/test_update_security.py::test_root_metadata_threshold_rotation_and_release_authorization",
    ],
    "channel": [
        "tests/test_safe_update.py::test_manifest_keyring_channel_and_validity",
    ],
    "downgrade": [
        "tests/test_update_security.py::test_update_rejects_downgrade",
        "tests/test_release_hardening.py::test_bootstrap_refuses_to_downgrade_an_installed_system[0.0.2.96-0.0.2.95--True]",
        "tests/test_release_hardening.py::test_bootstrap_refuses_to_downgrade_an_installed_system[0.0.2.100-0.0.2.99--True]",
    ],
    "rotation": [
        "tests/test_update_security.py::test_root_metadata_threshold_rotation_and_release_authorization",
        "tests/test_safe_update.py::test_release_sequence_rejects_replay_and_equivocation",
    ],
    "recovery": [
        "tests/test_update_security.py::test_interrupted_update_recovery_after_restart_rolls_back",
        "tests/test_update_security.py::test_recover_if_interrupted_unreadable_status_with_evidence",
        "tests/test_update_security.py::test_recover_blocks_on_cancel_fence",
        "tests/test_update_security.py::test_rollback_invokes_vertep_script",
        "tests/test_update_security.py::test_process_request_triggers_rollback_on_failure",
        "tests/test_update_security.py::test_update_timeout_rolls_back_and_reports_correlated_error",
        "tests/test_update_security.py::test_run_fenced_stops_apply_and_delayed_side_effect_on_cancel_fence",
        "tests/test_update_security.py::test_run_fenced_kills_child_processes_of_apply",
    ],
    "unsafe_archive": [
        "tests/test_update_security.py::test_safe_extract_rejects_symlink_member",
        "tests/test_update_security.py::test_safe_extract_rejects_path_traversal_member",
        "tests/test_update_security.py::test_safe_extract_rejects_absolute_path_member",
        "tests/test_update_security.py::test_safe_extract_rejects_size_bomb",
        "tests/test_update_security.py::test_safe_extract_accepts_legitimate_archive",
    ],
    "symlink": [
        "tests/test_update_security.py::test_safe_extract_rejects_symlink_member",
    ],
    "cross_origin": [
        "tests/test_update_security.py::test_safe_extract_rejects_cross_origin_hardlink",
        "tests/test_update_security.py::test_safe_extract_rejects_path_traversal_member",
        "tests/test_update_security.py::test_safe_extract_rejects_absolute_path_member",
    ],
    "compatibility": [
        "tests/test_safe_update.py::test_rolling_compatibility_rejects_contract_migrations",
        "tests/test_safe_update.py::test_signed_manifest_and_compatibility",
    ],
}


def command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)


def docker_architecture(architecture: str) -> str:
    result = command("docker", "run", "--rm", "--platform", f"linux/{architecture}",
                     "alpine:3.20", "uname", "-m")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Docker platform probe failed")
    observed = result.stdout.strip()
    expected = EXPECTED_MACHINE[architecture]
    if observed != expected:
        raise RuntimeError(f"linux/{architecture} executor reported {observed!r}, expected {expected!r}")
    return observed


def junit_summary(path: Path) -> dict[str, int]:
    summary = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for case in ET.parse(path).getroot().iter("testcase"):
        if case.find("failure") is not None:
            summary["failed"] += 1
        elif case.find("error") is not None:
            summary["error"] += 1
        elif case.find("skipped") is not None:
            summary["skipped"] += 1
        else:
            summary["passed"] += 1
    return summary


def executor_command(architecture: str, output: Path) -> list[str]:
    """Run the executor trust matrix inside a ``linux/<architecture>`` container."""
    script = (
        "set -euo pipefail\n"
        "apt-get update -qq >/dev/null\n"
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssl >/dev/null\n"
        "python -m pip install -q --disable-pip-version-check -r requirements.txt\n"
        "python -c 'import platform; print(platform.machine())' > /evidence/machine\n"
        "python -m pytest -q -p no:cacheprovider -o junit_family=legacy "
        "--basetemp=/tmp/executor --junitxml=/evidence/executor.xml " + " ".join(EXECUTOR_TARGETS) + "\n"
    )
    return ["docker", "run", "--rm", "--platform", f"linux/{architecture}",
            "-v", f"{ROOT}:/src", "-v", f"{output}:/evidence", "-w", "/src",
            "-e", "PYTHONDONTWRITEBYTECODE=1", EXECUTOR_IMAGE, "bash", "-c", script]


def criteria(executor: dict, host: dict, architecture: str) -> dict[str, str]:
    executor_ok = (executor.get("returncode") == 0
                   and executor.get("machine") == EXPECTED_MACHINE[architecture]
                   and executor.get("pytest", {}).get("passed", 0) > 0
                   and not any(executor.get("pytest", {}).get(key) for key in ("failed", "error", "skipped")))
    host_ok = (host.get("returncode") == 0 and host.get("pytest", {}).get("passed", 0) > 0
               and not any(host.get("pytest", {}).get(key) for key in ("failed", "error", "skipped")))
    # B2 is PASS only when every trust row passed on this architecture, never from totals.
    # B3 is proven by the Ubuntu bootstrap job (qualify_ubuntu_bootstrap.py), not here.
    statuses = {row["status"] for row in
                b4_dimension_report(executor, host, architecture)["dimensions"].values()}
    if executor_ok and host_ok and statuses == {"PASS"}:
        b2 = "PASS"
    else:
        b2 = "PARTIAL" if statuses - {"NOT_RUN"} else "FAIL"
    b4 = "PASS" if b2 == "PASS" else ("PARTIAL" if statuses - {"NOT_RUN"} else "FAIL")
    return {"B1": "NOT_RUN", "B2": b2, "B3": "NOT_RUN", "B4": b4}


def b4_dimension_report(executor: dict, host: dict, architecture: str) -> dict:
    """Produce a B4 criterion-level report: per-dimension test IDs, observed
    executor architecture, installed runtime, and NOT_RUN/PARTIAL/PASS status.

    This satisfies the B4 requirement from Issue #391: each dimension maps to its
    concrete test IDs, the harness SHA, the observed executor architecture, and the
    result status.  Dimensions whose tests were not executed are labelled NOT_RUN.
    """
    executor_ran = (executor.get("returncode") is not None
                    and executor.get("machine") == EXPECTED_MACHINE.get(architecture))
    host_ran = host.get("returncode") is not None

    # Exact IDs that passed inside the executor container; skipped is not passed.
    passed_ids: set[str] = set()
    junit_path = executor.get("junit")
    if executor_ran and junit_path and Path(junit_path).is_file():
        for case in ET.parse(junit_path).getroot().iter("testcase"):
            if all(case.find(tag) is None for tag in ("failure", "error", "skipped")):
                module = case.get("classname", "").replace(".", "/")
                passed_ids.add(f"{module}.py::{case.get('name', '')}")

    dimensions: dict[str, dict] = {}
    for dimension, test_ids in EXECUTOR_TRUST_DIMENSIONS.items():
        passed = [test_id for test_id in test_ids if test_id in passed_ids]
        status = "PASS" if len(passed) == len(test_ids) else ("PARTIAL" if passed else "NOT_RUN")
        dimensions[dimension] = {
            "status": status,
            "test_ids": test_ids,
            "missing": [test_id for test_id in test_ids if test_id not in passed_ids],
            "executor_architecture": executor.get("machine", "NOT_RUN"),
            "host_architecture": host.get("machine", "NOT_RUN"),
        }

    return {
        "dimensions": dimensions,
        "executor_ran": executor_ran,
        "host_ran": host_ran,
        "executor_machine": executor.get("machine", "NOT_RUN"),
        "host_machine": host.get("machine", "NOT_RUN"),
        "executor_image": executor.get("image", ""),
        "executor_pytest": executor.get("pytest", {}),
        "host_pytest": host.get("pytest", {}),
        "note": (
            "B4 criterion-level report: per-dimension test IDs, observed executor "
            "architecture, and NOT_RUN/PARTIAL/PASS status per Issue #391 B4."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=tuple(EXPECTED_MACHINE), required=True)
    parser.add_argument("--junit", type=Path, required=True,
                        help="JUnit of the host Docker executor run")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report: dict[str, object] = {
        "issue": 388,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sha": command("git", "rev-parse", "HEAD").stdout.strip(),
        "environment": {"system": platform.platform(), "python": platform.python_version(),
                        "ci": bool(os.getenv("GITHUB_ACTIONS")), "architecture": args.architecture,
                        "host_machine": platform.machine()},
        "criteria": {"B1": "NOT_RUN", "B2": "FAIL", "B3": "NOT_RUN", "B4": "FAIL"},
        "executor_targets": list(EXECUTOR_TARGETS),
        "host_targets": list(HOST_TARGETS),
        "trust_dimensions": list(EXECUTOR_TRUST_DIMENSIONS.keys()),
    }
    executor: dict = {}
    host: dict = {}
    try:
        report["environment"]["container_architecture"] = docker_architecture(args.architecture)
        # The host executor runs first: the root executor container writes into the
        # mounted checkout (e.g. logs/), which the unprivileged host run could not reopen.
        args.junit.parent.mkdir(parents=True, exist_ok=True)
        result = command(sys.executable, "-m", "pytest", "-q", "-o", "junit_family=legacy",
                         f"--junitxml={args.junit}", *HOST_TARGETS)
        host = {"machine": platform.machine(), "returncode": result.returncode,
                "pytest": junit_summary(args.junit)}

        evidence = args.output.parent.resolve() / f"executor-{args.architecture}"
        evidence.mkdir(parents=True, exist_ok=True)
        result = command(*executor_command(args.architecture, evidence))
        executor = {"image": EXECUTOR_IMAGE, "returncode": result.returncode,
                    "log_tail": (result.stdout + result.stderr)[-4000:]}
        if (evidence / "machine").is_file():
            executor["machine"] = (evidence / "machine").read_text(encoding="utf-8").strip()
        if (evidence / "executor.xml").is_file():
            executor["pytest"] = junit_summary(evidence / "executor.xml")
            executor["junit"] = str(evidence / "executor.xml")
        report["criteria"] = criteria(executor, host, args.architecture)
        report["b4_report"] = b4_dimension_report(executor, host, args.architecture)
    except (OSError, RuntimeError, ET.ParseError) as error:
        report["error"] = str(error)
    report["executor"] = executor
    report["host_executor"] = host
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["criteria"]["B2"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
