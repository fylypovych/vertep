#!/usr/bin/env python3
"""Disposable Ubuntu 24.04 bootstrap lifecycle evidence for Issue #388 B3.

Run as root on a throwaway Ubuntu 24.04 host (the CI runner).  The harness runs the
checked-out ``bootstrap.sh`` for real: it installs the pinned container runtime,
resolves and verifies the latest *published* signed release, and starts its
containers.  It then proves, against the running appliance:

* every container is running/healthy;
* a TLS handshake on :8443 presents exactly the installed certificate, and
  ``/api/health`` answers over that HTTPS connection;
* a certificate renewal through the certificate manager is picked up by the proxy
  reload without restarting it;
* data written to PostgreSQL and storage, the generated credentials and the
  renewed certificate survive a second bootstrap run, after which HTTPS and
  health still work.

The installed version is the latest published release, not this checkout; the
evidence records both.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import socket
import ssl
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = Path(os.getenv("VERTEP_INSTALL_ROOT", "/opt/vertep"))
SECRET_KEY = re.compile(r"(PASSWORD|SECRET|_KEY|TOKEN)")
MARKER = f"bootstrap-lifecycle-{os.getpid()}"


def run(*command: str, check: bool = True, timeout: int = 3600, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    if check and result.returncode:
        raise RuntimeError(f"{command[0]} failed ({result.returncode}): "
                           f"{(result.stdout + result.stderr)[-3000:]}")
    return result


def env_values() -> dict[str, str]:
    values = {}
    for line in (INSTALL_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


def compose(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return run("docker", "compose", "--env-file", str(INSTALL_ROOT / ".env"),
               "-f", str(INSTALL_ROOT / "docker-compose.yml"), *arguments, check=check)


def bootstrap(log: Path) -> dict:
    environment = {**os.environ, "VERTEP_ROLE": "core", "VERTEP_WEB_DOMAIN": "vertep.ci",
                   "DEBIAN_FRONTEND": "noninteractive"}
    started = time.monotonic()
    result = subprocess.run(["bash", str(ROOT / "bootstrap.sh")], cwd=ROOT, env=environment,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3600)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"bootstrap failed ({result.returncode}): {(result.stdout + result.stderr)[-3000:]}")
    return {"seconds": round(time.monotonic() - started), "log": str(log)}


def containers() -> list[dict]:
    rows = compose("ps", "--all", "--format", "json").stdout.strip()
    parsed = [json.loads(line) for line in rows.splitlines() if line.strip().startswith("{")]
    if not parsed and rows.startswith("["):
        parsed = json.loads(rows)
    return [{"service": row.get("Service"), "state": row.get("State"), "health": row.get("Health"),
             "image": row.get("Image")} for row in parsed]


def containers_ready(rows: list[dict]) -> bool:
    serving = [row for row in rows if row["service"] != "migrate"]
    return bool(serving) and all(row["state"] == "running" and row["health"] in ("", "healthy", None)
                                 for row in serving)


def certificate_sha256() -> str:
    pem = (INSTALL_ROOT / "tls" / "vertep.crt").read_text(encoding="ascii")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def handshake() -> dict:
    """TLS handshake verified against the installed certificate, plus /api/health."""
    context = ssl.create_default_context(cafile=str(INSTALL_ROOT / "tls" / "vertep.crt"))
    context.check_hostname = False  # the chain is verified; the fingerprint is compared below
    with socket.create_connection(("127.0.0.1", 8443), timeout=15) as raw:
        with context.wrap_socket(raw, server_hostname="vertep.ci") as tls:
            presented = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            tls.sendall(b"GET /api/health HTTP/1.1\r\nHost: vertep.ci\r\nConnection: close\r\n\r\n")
            response = b""
            while chunk := tls.recv(65536):
                response += chunk
            version = tls.version()
    status_line = response.split(b"\r\n", 1)[0].decode("ascii", "replace")
    return {"presented_sha256": presented, "tls_version": version, "health_status_line": status_line}


def wait(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (OSError, RuntimeError, ssl.SSLError) as error:
            last = error
        time.sleep(3)
    raise RuntimeError(f"timed out waiting for {what}: {last}")


def secret_fingerprints() -> dict[str, str]:
    return {key: hashlib.sha256(value.encode()).hexdigest()[:16]
            for key, value in env_values().items()
            if SECRET_KEY.search(key) and "SETUP" not in key and value}


def write_markers() -> None:
    compose("exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "vertep", "-d", "vertep", "-c",
            f"CREATE TABLE IF NOT EXISTS ci_bootstrap_marker(value text); "
            f"INSERT INTO ci_bootstrap_marker VALUES ('{MARKER}');")
    (INSTALL_ROOT / "storage" / "ci-bootstrap-marker.txt").write_text(MARKER, encoding="utf-8")


def markers_present() -> dict:
    rows = compose("exec", "-T", "postgres", "psql", "-U", "vertep", "-d", "vertep", "-Atc",
                   f"SELECT count(*) FROM ci_bootstrap_marker WHERE value='{MARKER}'").stdout.strip()
    stored = INSTALL_ROOT / "storage" / "ci-bootstrap-marker.txt"
    return {"postgres_rows": int(rows or 0),
            "storage_file": stored.is_file() and stored.read_text(encoding="utf-8") == MARKER}


def renew_certificate() -> None:
    code = ("import urllib.request; urllib.request.urlopen(urllib.request.Request("
            "'http://certificate-manager:8096/certificate/renew', method='POST'), timeout=60).read()")
    compose("exec", "-T", "core", "python3", "-c", code)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    evidence_dir = arguments.output.parent
    evidence_dir.mkdir(parents=True, exist_ok=True)
    os_release = dict(line.split("=", 1) for line in
                      Path("/etc/os-release").read_text().splitlines() if "=" in line)
    report: dict = {
        "issue": 388, "criterion": "B3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Runs as root in a checkout owned by the runner user.
        "sha": run("git", "-c", f"safe.directory={ROOT}", "rev-parse", "HEAD", cwd=ROOT).stdout.strip(),
        "environment": {"os": os_release.get("PRETTY_NAME", "").strip('"'),
                        "machine": platform.machine(), "kernel": platform.release()},
        "steps": {}, "result": "FAIL",
    }
    steps = report["steps"]
    try:
        steps["bootstrap"] = bootstrap(evidence_dir / "bootstrap-first.log")
        report["installed_version"] = env_values().get("VERTEP_VERSION")
        steps["containers"] = wait(lambda: (rows := containers()) and containers_ready(rows) and rows,
                                   600, "healthy containers")
        first = wait(lambda: (value := handshake()) and " 200 " in value["health_status_line"] + " "
                     and value, 300, "HTTPS health")
        installed = certificate_sha256()
        assert first["presented_sha256"] == installed, "proxy presents a different certificate"
        steps["https"] = {**first, "installed_sha256": installed}

        renew_certificate()
        renewed = certificate_sha256()
        assert renewed != installed, "renewal did not replace the certificate"
        reloaded = wait(lambda: (value := handshake())["presented_sha256"] == renewed and value,
                        120, "proxy reload with the renewed certificate")
        steps["renewal"] = {"before_sha256": installed, "after_sha256": renewed,
                            "presented_sha256": reloaded["presented_sha256"],
                            "health_status_line": reloaded["health_status_line"]}

        write_markers()
        secrets_before = secret_fingerprints()
        steps["rerun"] = bootstrap(evidence_dir / "bootstrap-rerun.log")
        steps["containers_after_rerun"] = wait(
            lambda: (rows := containers()) and containers_ready(rows) and rows, 600,
            "healthy containers after the rerun")
        after = wait(lambda: (value := handshake()) and " 200 " in value["health_status_line"] + " "
                     and value, 300, "HTTPS health after the rerun")
        persisted = markers_present()
        secrets_after = secret_fingerprints()
        steps["persistence"] = {
            **persisted, "certificate_kept": after["presented_sha256"] == renewed,
            "credentials_kept": secrets_before == secrets_after,
            "changed_credentials": sorted(key for key in secrets_before
                                          if secrets_before.get(key) != secrets_after.get(key)),
            "health_status_line": after["health_status_line"]}
        assert persisted["postgres_rows"] == 1 and persisted["storage_file"], "data was lost on rerun"
        assert steps["persistence"]["certificate_kept"], "rerun replaced the renewed certificate"
        assert steps["persistence"]["credentials_kept"], "rerun changed generated credentials"
        report["result"] = "PASS"
    except (AssertionError, RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        report["error"] = str(error)[-4000:]
    finally:
        logs = compose("logs", "--no-color", "--tail", "200", check=False)
        (evidence_dir / "compose-logs.txt").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: report.get(key) for key in ("result", "installed_version", "error")}))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
