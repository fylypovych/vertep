#!/usr/bin/env python3
"""Fail-closed disposable bootstrap/update acceptance evidence for Issue #384.

This harness does not call the publishing release workflow.  It proves only
the non-publishing Docker executor boundary available on a CI runner and
writes machine-readable evidence for the exact checked-out SHA.
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
TARGETS = (
    "tests/test_apply_deployment_docker.py",
    "tests/test_safe_update.py",
    "tests/test_update_security.py",
    "tests/test_bootstrap_wizard.py",
)


def command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)


def docker_architecture(architecture: str) -> str:
    result = command("docker", "run", "--rm", "--platform", f"linux/{architecture}",
                     "alpine:3.20", "uname", "-m")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Docker platform probe failed")
    observed = result.stdout.strip()
    expected = {"amd64": "x86_64", "arm64": "aarch64"}[architecture]
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report: dict[str, object] = {
        "issue": 384,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sha": command("git", "rev-parse", "HEAD").stdout.strip(),
        "environment": {"system": platform.platform(), "python": platform.python_version(),
                        "ci": bool(os.getenv("GITHUB_ACTIONS")), "architecture": args.architecture},
        "criteria": {"B1": "NOT_RUN", "B2": "FAIL", "B3": "NOT_RUN", "B4": "FAIL"},
        "targets": list(TARGETS),
    }
    try:
        report["environment"]["container_architecture"] = docker_architecture(args.architecture)
        args.junit.parent.mkdir(parents=True, exist_ok=True)
        result = command(sys.executable, "-m", "pytest", "-q", "-o", "junit_family=legacy",
                         f"--junitxml={args.junit}", *TARGETS)
        summary = junit_summary(args.junit)
        report["pytest"] = summary
        report["criteria"]["B2"] = "PASS" if result.returncode == 0 and not summary["skipped"] else "FAIL"
        # The selected tests deliberately cover bootstrap/update executor and trust faults.
        # They do not claim a real TLS proxy reload/rerun; B3 stays NOT_RUN.
        report["criteria"]["B4"] = "PARTIAL" if report["criteria"]["B2"] == "PASS" else "FAIL"
    except (OSError, RuntimeError, ET.ParseError) as error:
        report["error"] = str(error)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["criteria"]["B2"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
