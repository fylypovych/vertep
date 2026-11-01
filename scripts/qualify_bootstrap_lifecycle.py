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
EXECUTOR_TARGETS = (
    "tests/test_safe_update.py",
    "tests/test_update_security.py",
    "tests/test_bootstrap_wizard.py",
    "tests/test_release_hardening.py",
)
HOST_TARGETS = ("tests/test_apply_deployment_docker.py",)
EXPECTED_MACHINE = {"amd64": "x86_64", "arm64": "aarch64"}
EXECUTOR_IMAGE = "python:3.12-slim"


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
    b2 = "PASS" if executor_ok and host_ok else "FAIL"
    # The selected tests deliberately cover bootstrap/update executor and trust faults.
    # They do not claim a real TLS proxy reload/rerun; B3 stays NOT_RUN.
    return {"B1": "NOT_RUN", "B2": b2, "B3": "NOT_RUN", "B4": "PARTIAL" if b2 == "PASS" else "FAIL"}


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
    }
    executor: dict = {}
    host: dict = {}
    try:
        report["environment"]["container_architecture"] = docker_architecture(args.architecture)
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

        args.junit.parent.mkdir(parents=True, exist_ok=True)
        result = command(sys.executable, "-m", "pytest", "-q", "-o", "junit_family=legacy",
                         f"--junitxml={args.junit}", *HOST_TARGETS)
        host = {"machine": platform.machine(), "returncode": result.returncode,
                "pytest": junit_summary(args.junit)}
        report["criteria"] = criteria(executor, host, args.architecture)
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
