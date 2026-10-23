"""Actual Browser-to-CORE Runner acceptance, including CORE process recovery."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    if os.getenv("VERTEP_REQUIRE_RUNNER_BROWSER_E2E") == "1":
        raise
    pytest.skip("Playwright is not installed", allow_module_level=True)


ROOT = Path(__file__).resolve().parents[1]
ADMIN_USER = "runner-e2e-admin"
ADMIN_PASSWORD = "runner-e2e-password-12"


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_for_core(process: subprocess.Popen, url: str, log_path: Path) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=10) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    log = log_path.read_text(encoding="utf-8", errors="replace")
    raise AssertionError(f"disposable CORE did not become healthy\n{log[-6000:]}")


def _start_core(port: int, config_root: Path, storage_root: Path,
                run_storage: Path, log_path: Path, env: dict[str, str]):
    log_handle = log_path.open("ab")
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "core.app:app",
            "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=ROOT,
        env={
            **env,
            "CONFIG_ROOT": str(config_root),
            "STORAGE_ROOT": str(storage_root),
            "JOB_ROOT": str(storage_root / "jobs"),
            "REAL_TESTS_STORAGE_DIR": str(run_storage),
            "ADMIN_USER": ADMIN_USER,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
            "SECRET_STORE_PASSPHRASE": "runner-browser-test-passphrase",
            "USERS_JSON": "{}",
            "NODE_ROLE": "core",
            "RATE_LIMIT_PER_MINUTE": "100000",
        },
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    _wait_for_core(process, f"http://127.0.0.1:{port}", log_path)
    return process, log_handle


def _login(page, base_url: str) -> None:
    page.goto(f"{base_url}/login")
    expect(page.get_by_role("heading", name="Вхід")).to_be_visible(timeout=20000)
    page.locator("input[type='text']").fill(ADMIN_USER)
    page.locator("input[type='password']").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Увійти").click()
    page.wait_for_url(lambda url: "/login" not in url, timeout=20000)


def _save_evidence(evidence: dict) -> None:
    output = os.getenv("VERTEP_RUNNER_BROWSER_EVIDENCE", "").strip()
    if not output:
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def test_runner_web_actual_api_progress_history_restart_and_retry(tmp_path):
    required = os.getenv("VERTEP_REQUIRE_RUNNER_BROWSER_E2E") == "1"
    ui_root = ROOT / "web-v2" / "dist" / "vertep-admin-v2"
    if not ui_root.is_dir():
        if required:
            pytest.fail("required Angular production build is missing")
        pytest.skip("build web-v2 before running the actual-backend Browser acceptance")

    port = _port()
    base_url = f"http://127.0.0.1:{port}"
    config_root = tmp_path / "config"
    storage_root = tmp_path / "storage"
    run_storage = tmp_path / "real-tests"
    config_root.mkdir()
    storage_root.mkdir()
    (config_root / "installation.json").write_text(
        json.dumps({"completed_at": "2026-10-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    log_path = tmp_path / "core.log"
    process_env = dict(os.environ)
    process_env.pop("VERTEP_RELEASE_IDENTITY_FILE", None)

    process, log_handle = _start_core(
        port, config_root, storage_root, run_storage, log_path, process_env,
    )
    browser = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page_errors: list[str] = []
            console_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error" and not msg.text.startswith("Failed to load resource:")
                else None,
            )
            _login(page, base_url)
            page.goto(f"{base_url}/real-tests")
            expect(page.locator("[data-testid='real-tests-page']")).to_be_visible(timeout=20000)

            scenario = page.locator("[data-testid='real-tests-scenario-S01']")
            expect(scenario).to_be_visible()
            scenario.get_by_role("button", name="Запустити").click()
            dialog = page.locator("[data-testid='real-tests-confirm']")
            expect(dialog).to_be_visible()
            with page.expect_response(
                lambda response: response.url.endswith("/api/real-tests/run")
                and response.request.method == "POST"
            ) as run_response:
                dialog.get_by_role("button", name="Запустити").click()
            response = run_response.value
            assert response.status == 200, response.text()
            run = response.json()
            assert run["status"] == "RUNNING"
            assert run["progress"] == "started: 0/1 checks"
            run_id = run["test_run_id"]
            expect(scenario.locator("[data-testid='real-tests-progress']")).to_contain_text(
                "checks", timeout=10000,
            )

            expect(scenario.get_by_role("button", name="Запустити")).to_be_enabled(timeout=30000)
            page.reload()
            page.get_by_role("button", name="Історія").click()
            row = page.locator(f"[data-testid='real-tests-run-{run_id}']")
            expect(row).to_be_visible(timeout=15000)
            expect(row).to_contain_text(ADMIN_USER)
            expect(row).to_contain_text("progress:")

            stored = page.request.get(f"{base_url}/api/real-tests/runs/{run_id}/progress")
            assert stored.status == 200, stored.text()
            first_progress = stored.json()
            assert first_progress["status"] != "RUNNING"
            assert first_progress["progress"].startswith("progress:")

            process.terminate()
            process.wait(timeout=15)
            log_handle.close()
            process, log_handle = _start_core(
                port, config_root, storage_root, run_storage, log_path, process_env,
            )

            page.reload()
            page.get_by_role("button", name="Історія").click()
            row = page.locator(f"[data-testid='real-tests-run-{run_id}']")
            expect(row).to_be_visible(timeout=15000)
            expect(row).to_contain_text(ADMIN_USER)
            expect(row).to_contain_text(first_progress["progress"])
            row.get_by_role("button", name="Звіт").click()
            report = page.locator("[data-testid='real-tests-report']")
            expect(report).to_be_visible()
            expect(report).to_contain_text("bootstrap_first_run")
            expect(report).to_contain_text("SHA:")

            exact_sha = os.getenv("GITHUB_SHA", "").strip().lower()
            if exact_sha:
                assert re.fullmatch(r"[0-9a-f]{40}", exact_sha)
                expect(report).to_contain_text(exact_sha)
            else:
                report_text = report.inner_text()
                assert re.search(r"SHA:\s*[0-9a-f]{40}", report_text), report_text

            retry_response = None
            retry_button = page.get_by_role("button", name="Повторити відправку до GitHub")
            expect(retry_button).to_be_visible()
            with page.expect_response(
                lambda response: response.url.endswith(f"/api/real-tests/runs/{run_id}/retry-report")
                and response.request.method == "POST"
            ) as retry_response_info:
                retry_button.click()
            retry_response = retry_response_info.value
            assert retry_response.status == 200, retry_response.text()

            final_run = page.request.get(f"{base_url}/api/real-tests/runs/{run_id}")
            assert final_run.status == 200, final_run.text()
            final_report = final_run.json()
            assert final_report["initiator"] == ADMIN_USER
            assert final_report["progress"].startswith("progress:")
            assert final_report["checks"][0]["name"] == "bootstrap_first_run"
            assert page_errors == [], f"pageerror: {page_errors}"
            assert console_errors == [], f"console.error: {console_errors}"
            _save_evidence({
                "criterion": "i.0.0.2.85 / #188.1-#188.2",
                "test_run_id": run_id,
                "expected": {
                    "status": "terminal after execution and process restart",
                    "initiator": ADMIN_USER,
                    "report_retry_http": 200,
                    "runner_api_mocked": False,
                    "exact_sha": exact_sha or "full SHA from runtime",
                },
                "actual": {
                    "status": final_report["status"],
                    "result": final_report["result"],
                    "progress": final_report["progress"],
                    "initiator": final_report["initiator"],
                    "commit_sha": final_report["commit"],
                    "report_retry_http": retry_response.status,
                    "process_restarted": True,
                    "runner_api_mocked": False,
                },
                "result": "PASS",
            })
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        if not log_handle.closed:
            log_handle.close()
