"""Update Center Browser acceptance over the standard executor route (#401 F3).

The production web-v2 build is served by a real CORE process. The Browser mutation is
applied by the real update agent through the standard bash consumer (``scripts/vertep``)
to a Docker installation of a running Vertep worker: a signed release, the legacy rescue,
activation and the container recreate. A failed apply is rolled back and a retry
succeeds; the Browser shows each real operation result. Nothing on the update route is
answered by ``page.route``.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.test_standard_host_consumer_docker import (  # noqa: F401
    IMAGES, _agent, _archive, _payload, _pinned, _run, _status, installation)

ROOT = Path(__file__).resolve().parents[1]
REQUIRE = os.getenv("VERTEP_REQUIRE_UPDATE_BROWSER_E2E") == "1"
ADMIN_PASSWORD = "standard-update-browser-admin"

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - dependency is required in CI
    if REQUIRE:
        pytest.fail("playwright is not installed; Update Browser proof is required", pytrace=False)
    pytest.skip("playwright is not installed", allow_module_level=True)


def _require_web_build() -> None:
    if any((ROOT / path / "index.html").is_file() for path in (
            "web-v2/dist/vertep-admin-v2/browser", "web-v2/dist/vertep-admin-v2")):
        return
    message = "web-v2 production build is missing; run `npm run build` in web-v2"
    if REQUIRE:
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


@pytest.fixture
def browser_core(tmp_path):
    _require_web_build()
    config, state_dir = tmp_path / "core-config", tmp_path / "update-state"
    config.mkdir()
    state_dir.mkdir()
    (config / "users.json").write_text("{}", encoding="utf-8")
    (config / "installation.json").write_text('{"completed_at":"2026-01-01"}', encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "CONFIG_ROOT": str(config), "UPDATE_STATE_DIR": str(state_dir),
           "JOB_ROOT": str(tmp_path / "core-jobs"), "SYSTEM_STATE_BACKEND": "file",
           "WEB_UPDATE_ENABLED": "true", "ADMIN_USER": "admin", "ADMIN_PASSWORD": ADMIN_PASSWORD,
           "COOKIE_SECURE": "false", "DATABASE_URL": "", "REDIS_URL": "", "USERS_JSON": ""}
    log = (tmp_path / "core.log").open("w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "core.app:app", "--host",
                                "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env,
                               stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/api/health", timeout=2).status_code < 500:
                break
        except httpx.HTTPError:
            pass
        assert process.poll() is None, (tmp_path / "core.log").read_text(encoding="utf-8")
        time.sleep(0.5)
    yield {"url": url, "state_dir": state_dir}
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
    log.close()


def test_update_browser_drives_the_standard_executor_with_rollback_and_retry(
        browser_core, installation, signed_update_server, tmp_path, monkeypatch):  # noqa: F811
    root, state_dir, url = installation.root, browser_core["state_dir"], browser_core["url"]
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()
    before = installation.container()
    good = _pinned(IMAGES["0.0.0.2"])

    def apply_queued() -> bool:
        requests = sorted((state_dir / "requests").glob("*.json"))
        assert len(requests) == 1, requests
        return agent.process_request(root, state_dir, requests[0], skip_drain=True)

    credentials = base64.b64encode(f"admin:{ADMIN_PASSWORD}".encode()).decode()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        assert context.request.post(f"{url}/api/session",
                                    headers={"Authorization": f"Basic {credentials}"}).ok
        page.goto(f"{url}/settings?tab=update")
        expect(page.get_by_test_id("settings-update")).to_be_visible()

        def install() -> str:
            with page.expect_response(lambda response: "/api/system/update/run" in response.url) as run:
                page.get_by_test_id("update-install").click()
            assert run.value.ok, run.value.text()
            return run.value.json()["request_id"]

        # 1) A release whose image digest Docker cannot pull: the standard consumer fails
        #    and the agent rolls the running worker back; the Browser shows the recovery.
        signed_update_server.publish("0.0.0.2", _archive(_payload(
            tmp_path / "release-broken", "0.0.0.2", {**good, "digest": "sha256:" + "0" * 64})))
        failed_id = install()
        assert apply_queued() is False
        failed = _status(state_dir)
        assert failed["request_id"] == failed_id and failed["state"] in {"FAILED", "ROLLED_BACK"}
        assert IMAGES["0.0.0.1"] in _run("docker", "inspect", "--format", "{{.Config.Image}}",
                                         installation.container())
        page.reload()
        expect(page.get_by_test_id("update-recovery-state")).to_be_visible()

        # 2) Retry from the Browser with a corrected release: a new request, the worker is
        #    recreated on 0.0.0.2 and the Browser shows the completed operation.
        signed_update_server.publish("0.0.0.2", _archive(_payload(
            tmp_path / "release-fixed", "0.0.0.2", good)))
        retry_id = install()
        assert retry_id != failed_id
        assert apply_queued() is True, json.dumps(_status(state_dir))[:2000]
        status = _status(state_dir)
        assert status["request_id"] == retry_id and status["state"] == "SUCCEEDED"
        updated = installation.container()
        assert updated and updated != before
        # The recreated container runs the image the activated release pins (the
        # bind-mounted release file is cached by Docker Desktop, the image is not).
        assert IMAGES["0.0.0.2"] in _run("docker", "inspect", "--format", "{{.Config.Image}}", updated)
        page.reload()
        expect(page.get_by_test_id("update-progress")).to_contain_text("100%")
        assert page_errors == [], f"pageerror: {page_errors}"
        context.close()
        browser.close()
