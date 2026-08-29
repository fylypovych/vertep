#!/usr/bin/env python3
"""Run the post-update health gate and fail the update when any check fails."""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.health_gate import FINAL, POST_UPDATE, gate_report, probe_http  # noqa: E402


def core_report(core_url: str, mode: str, timeout: float) -> dict:
    url = f"{core_url.rstrip('/')}/api/system/health/full?{urlencode({'mode': mode})}"
    request = Request(url)
    password = os.getenv("ADMIN_PASSWORD", "")
    if password:
        credentials = f"{os.getenv('ADMIN_USER', 'admin')}:{password}"
        request.add_header("Authorization", "Basic " + base64.b64encode(credentials.encode()).decode())
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def worker_snapshot(expected_version: str) -> dict:
    from worker.service import configured_role, gpu_info, role_self_test
    metrics = gpu_info()
    demo = os.getenv("DEMO_MODE", "true").lower() == "true"
    comfyui_url = os.getenv("COMFYUI_URL", "")
    comfyui = {"required": bool(comfyui_url) and not demo, "reachable": False, "error": ""}
    if comfyui_url:
        probe = probe_http(comfyui_url.rstrip("/") + "/system_stats", timeout=5)
        comfyui.update({"reachable": bool(probe.get("reachable")), "error": probe.get("error", "")})
    return {"gpu_required": os.getenv("WORKER_REQUIRE_GPU", "true").lower() == "true" and not demo,
            "gpu_available": bool(metrics.get("gpu_available")), "gpu_name": metrics.get("gpu_name", "unknown"),
            "driver_version": metrics.get("driver_version", "unknown"),
            "cuda_version": metrics.get("cuda_version", "unknown"),
            "free_vram_mb": metrics.get("free_vram_mb", metrics.get("vram_mb", 0)),
            "vram_mb": metrics.get("vram_mb", 0),
            "min_vram_mb": int(os.getenv("WORKER_MIN_FREE_VRAM_MB", "0")),
            "comfyui": comfyui, "worker_api": {"reachable": True},
            "self_test": role_self_test(configured_role(), metrics), "version": expected_version}


def render(report: dict) -> str:
    lines = [f"Health gate {report['mode']} for {report['role']} {report['version']}: "
             f"{'PASSED' if report['passed'] else 'FAILED'}"]
    for check in report["checks"]:
        marker = "ok  " if check["passed"] else ("FAIL" if check["critical"] else "warn")
        lines.append(f"  [{marker}] {check['component']}/{check['name']}: {check['detail']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Vertep post-update health gate")
    parser.add_argument("--role", required=True, help="core, worker or core-worker")
    parser.add_argument("--mode", default=POST_UPDATE, choices=[POST_UPDATE, FINAL])
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--core-url", default=os.getenv("VERTEP_CORE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("HEALTH_GATE_TIMEOUT_SECONDS", "180")))
    parser.add_argument("--interval", type=float, default=float(os.getenv("HEALTH_GATE_INTERVAL_SECONDS", "5")))
    parser.add_argument("--report", type=Path, default=None, help="Write the last report as JSON")
    arguments = parser.parse_args()

    deadline = time.monotonic() + arguments.timeout
    report = {"passed": False, "role": arguments.role, "mode": arguments.mode,
              "version": arguments.expected_version, "failed": ["startup"], "checks": [],
              "checked_at": datetime.now(timezone.utc).isoformat()}
    while True:
        try:
            reports = []
            if arguments.role in {"core", "core-worker"}:
                reports.append(("core", core_report(arguments.core_url, arguments.mode,
                                                    min(30.0, arguments.timeout))))
            if arguments.role in {"worker", "core-worker"}:
                reports.append(("worker", gate_report("worker", worker_snapshot(arguments.expected_version),
                                                      arguments.expected_version, arguments.mode)))
            report = {**reports[0][1], "role": arguments.role, "version": arguments.expected_version,
                      "checks": [check for _, item in reports for check in item["checks"]],
                      "failed": [name for _, item in reports for name in item["failed"]]}
            # A service that never restarted answers happily about its own old release,
            # so the reported version is compared against the release being installed.
            for report_role, item in reports:
                if item.get("version") != arguments.expected_version:
                    report["checks"].append(
                        {"name": "release_version", "component": report_role, "passed": False, "critical": True,
                         "detail": f"{report_role} reports {item.get('version')}, expected {arguments.expected_version}"})
                    report["failed"].append("release_version")
            report["passed"] = not report["failed"]
        except Exception as error:  # network, auth and runtime failures are gate failures
            report = {**report, "failed": ["unreachable"], "checked_at": datetime.now(timezone.utc).isoformat(),
                      "passed": False,
                      "checks": [{"name": "unreachable", "component": arguments.role, "passed": False,
                                  "critical": True, "detail": str(error)}]}
        if report["passed"] or time.monotonic() >= deadline:
            break
        time.sleep(arguments.interval)

    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render(report), file=sys.stderr if not report["passed"] else sys.stdout)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
