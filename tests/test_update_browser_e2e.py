"""Real-backend Browser acceptance for the Web Update Center (Issue #254).

Unlike the mocked Update Center browser tests, this scenario runs the actual
CORE process, the actual owner mutation endpoint, the actual update-agent
executor/fence/status/journal contract and the production web-v2 build.  Only
the network boundary (signed manifest/package fetch) and the platform command
name are represented by a disposable fixture; the privileged command is still
executed as a real child process through ``run_fenced`` and the real
update-agent state machine.

Criteria: #193.7 (mutation -> actual CORE -> operation state in Browser),
#193.8 (effective executor/fence, no direct status/version writes),
#193.9 (pageerror/console collectors, negative feedback, no skip in CI).

The host executor keeps the production request contract and replaces the active
runtime instance of a disposable installation.  The scenario runs on the Linux
CI runner, which provides the appliance platform together with Playwright and
the web-v2 build.  Locally without them it reports a skip; CI sets
``VERTEP_REQUIRE_UPDATE_BROWSER_E2E=1`` so a missing requirement fails instead
of silently passing.
"""

import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import time

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN_USER = "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "vertep-test-admin-secret")
REQUIRE = os.getenv("VERTEP_REQUIRE_UPDATE_BROWSER_E2E") == "1"

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - dependency is required in CI
    if REQUIRE:
        pytest.fail("playwright is not installed; Update Center Browser proof is required",
                    pytrace=False)
    pytest.skip("playwright is not installed", allow_module_level=True)
TARGET_VERSION = "9.9.9"
CURRENT_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _unsatisfied(message: str):
    if REQUIRE:
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def _web_dist() -> Path:
    for candidate in (ROOT / "web-v2/dist/vertep-admin-v2/browser",
                      ROOT / "web-v2/dist/vertep-admin-v2"):
        if (candidate / "index.html").is_file():
            return candidate
        if (candidate / "browser/index.html").is_file():
            return candidate / "browser"
    _unsatisfied("web-v2 production build is missing; run `npm run build` in web-v2")
    raise AssertionError("unreachable")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until(predicate, timeout=60, interval=0.2, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    pytest.fail(f"Timed out waiting for {message}")


@pytest.fixture(scope="module")
def disposable_core(tmp_path_factory):
    if os.name != "posix":
        _unsatisfied("the Update Center Browser acceptance runs on the Linux CI runner")
    _web_dist()  # fail closed before starting anything in required mode
    base = tmp_path_factory.mktemp("update-browser")
    state_dir = base / "update"
    config_dir = base / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "users.json").write_text("{}", encoding="utf-8")
    (config_dir / "installation.json").write_text(
        json.dumps({"completed_at": "2024-01-01T00:00:00Z"}), encoding="utf-8")
    port = _free_port()
    env = {
        **os.environ,
        "CONFIG_ROOT": str(config_dir),
        "UPDATE_STATE_DIR": str(state_dir),
        "SYSTEM_STATE_BACKEND": "file",
        "WEB_UPDATE_ENABLED": "true",
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "COOKIE_SECURE": "false",
    }
    env.pop("USERS_JSON", None)
    # The disposable CORE proves the update flow only; an inherited Redis URL
    # would make its health probe depend on a service that is not part of the
    # scenario.
    env.pop("REDIS_URL", None)
    log = (base / "core.log").open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "core.app:app", "--host", "127.0.0.1",
         "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"

    def healthy():
        try:
            return httpx.get(f"{url}/api/health", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False
    try:
        _wait_until(healthy, timeout=60, message="CORE to become healthy")
    except BaseException:
        log.close()
        process.terminate()
        pytest.fail((base / "core.log").read_text(encoding="utf-8", errors="replace"),
                    pytrace=False)
    yield {"url": url, "state_dir": state_dir, "root": base}
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    log.close()


def _release_package(base: Path, version: str) -> Path:
    """Build a real release archive for the disposable installation."""
    payload = base / f"payload-{version}"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "VERSION").write_text(version, encoding="utf-8")
    (payload / "application.txt").write_text(f"runtime {version}", encoding="utf-8")
    archive = base / f"vertep-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        package.add(payload, arcname=payload.name)
    return archive


def _run_host_agent(base: Path, root: Path, state_dir: Path, monkeypatch) -> bool:
    """Run the real update-agent for the queued Browser mutation."""
    spec = importlib.util.spec_from_file_location(
        "update_agent_browser_e2e", ROOT / "scripts" / "update-agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    import core.update_protocol as protocol
    monkeypatch.setattr(protocol, "fetch_manifest",
                        lambda _channel: {"version": TARGET_VERSION, "channel": "stable",
                                          "required": False})
    monkeypatch.setattr(protocol, "validate_manifest",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(protocol, "validate_replay_state",
                        lambda *_args, **_kwargs: None)
    archive = _release_package(base, TARGET_VERSION)
    monkeypatch.setattr(
        protocol, "download_package",
        lambda _manifest, path: (path.parent.mkdir(parents=True, exist_ok=True),
                                 shutil.copyfile(archive, path), path)[-1])
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    requests = sorted((state_dir / "requests").glob("*.json"))
    assert len(requests) == 1, f"expected one queued host request, found {requests}"
    return agent.process_request(root, state_dir, requests[0], skip_drain=True)


def _attach_collectors(page):
    page_errors, console_errors = [], []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    def console(message):
        if message.type == "error" and not message.text.startswith(
                "Failed to load resource:"):
            console_errors.append(message.text)
    page.on("console", console)
    return page_errors, console_errors


def test_update_center_browser_drives_real_core_and_host_executor(
        disposable_core, disposable_deployment, monkeypatch):
    base, state_dir = Path(disposable_core["root"]), disposable_core["state_dir"]
    root = disposable_deployment(base / "runtime", CURRENT_VERSION)
    url = disposable_core["url"]
    credentials = base64.b64encode(
        f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode()).decode()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page_errors, console_errors = _attach_collectors(page)
        session = context.request.post(
            f"{url}/api/session", headers={"Authorization": f"Basic {credentials}"})
        assert session.ok, session.text()
        page.goto(f"{url}/settings?tab=update")
        expect(page.get_by_test_id("settings-update")).to_be_visible()

        # 1) The Browser mutation reaches the real CORE state machine.
        with page.expect_response(
                lambda response: "/api/system/update/run" in response.url) as queued:
            page.get_by_test_id("update-install").click()
        assert queued.value.ok, queued.value.text()
        assert queued.value.json()["state"] == "PENDING"
        request_file = next((state_dir / "requests").glob("*.json"))
        assert json.loads(request_file.read_text(encoding="utf-8"))["action"] == "update"

        # 2) The real host executor applies the update; only then does the
        #    operation reach SUCCEEDED (nothing here writes the status directly).
        assert _run_host_agent(base, root, state_dir, monkeypatch) is True
        assert (root / "current" / "VERSION").read_text(encoding="utf-8").strip() == TARGET_VERSION
        assert not request_file.exists()
        status = httpx.get(f"{url}/api/system/update", auth=(ADMIN_USER, ADMIN_PASSWORD)).json()
        assert status["state"] == "SUCCEEDED" and status["progress"] == 100

        # 3) The Browser shows the real operation progress/result.
        page.reload()
        expect(page.get_by_test_id("update-progress")).to_contain_text("100%")
        expect(page.get_by_test_id("update-progress")).to_contain_text(
            "Update completed")

        # 4) Negative path: the real executor fails, the real rollback runs and
        #    the Browser reports recovery instead of a fabricated success.
        (root / "fail-apply").write_text("1", encoding="utf-8")
        with page.expect_response(
                lambda response: "/api/system/update/run" in response.url) as failed:
            page.get_by_test_id("update-install").click()
        assert failed.value.ok, failed.value.text()
        assert _run_host_agent(base, root, state_dir, monkeypatch) is False
        assert (root / "current" / "VERSION").read_text(encoding="utf-8").strip() == CURRENT_VERSION
        status = httpx.get(f"{url}/api/system/update", auth=(ADMIN_USER, ADMIN_PASSWORD)).json()
        assert status["state"] == "ROLLED_BACK"
        page.reload()
        expect(page.get_by_test_id("update-recovery-state")).to_be_visible()
        expect(page.get_by_test_id("update-progress")).to_contain_text("100%")

        assert page_errors == [], f"pageerror: {page_errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        context.close()
        browser.close()
