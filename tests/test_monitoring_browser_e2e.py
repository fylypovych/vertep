"""#357: real CORE process, heartbeat service, Browser and persistent alerts.

Existing retention/dedup/readiness/watchdog regressions: test_monitoring_api.py.
No alert records or probe results are inserted by these scenarios.
"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.test_media_browser_e2e import _free_port, _wait_until, _web_dist, sync_playwright, expect
from tests.acceptance_provenance import acceptance_provenance

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "monitoring-disposable-worker"
NODE = "monitoring-disposable"


@pytest.fixture
def monitoring_core(tmp_path):
    _web_dist()
    env = os.environ.copy()
    for name in ("CONFIG_ROOT", "JOB_ROOT", "STORAGE_ROOT", "UPDATE_STATE_DIR", "LOG_ROOT"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        env[name] = str(directory)
    config = Path(env["CONFIG_ROOT"])
    (config / "users.json").write_text("{}", encoding="utf-8")
    (config / "installation.json").write_text('{"completed_at":"2026-01-01"}', encoding="utf-8")
    env.update(NODE_API_TOKEN=TOKEN, HEARTBEAT_TIMEOUT="3", LOCAL_WORKER_FALLBACK="false",
               SYSTEM_STATE_BACKEND="file", ADMIN_PASSWORD="monitoring-test-password",
               REDIS_URL="", DATABASE_URL="", USERS_JSON="", RATE_LIMIT_PER_MINUTE="10000")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    auth = "Basic " + base64.b64encode(b"admin:monitoring-test-password").decode()
    log = (tmp_path / "core-process.log").open("w", encoding="utf-8")
    process = None

    def start():
        nonlocal process
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "core.app:app",
                                    "--host", "0.0.0.0", "--port", str(port)],
                                   cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        def ready():
            assert process.poll() is None, (tmp_path / "core-process.log").read_text(encoding="utf-8")
            try:
                return httpx.get(base + "/api/session", headers={"Authorization": auth}, timeout=5).status_code == 200
            except httpx.HTTPError:
                return False
        _wait_until(ready, message="disposable CORE process")

    def stop():
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=20)

    def restart():
        stop()
        start()

    try:
        start()
        with httpx.Client(base_url=base, headers={"Authorization": auth,
                          "x-vertep-token": TOKEN}, timeout=20) as api:
            yield {"url": base, "api": api, "auth": auth,
                   "restart": restart, "port": port, "root": tmp_path}
    finally:
        stop()
        log.close()


@pytest.fixture
def monitoring_page(monitoring_core):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(base_url=monitoring_core["url"])
        response = context.request.post("/api/session", headers={"Authorization": monitoring_core["auth"]})
        assert response.ok
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page
        assert errors == []
        context.close()
        browser.close()


def test_probe_failure_ack_recovery_survives_process_restart(monitoring_core, monitoring_page):
    core, page = monitoring_core, monitoring_page
    api = core["api"]
    # A real disposable service emits heartbeats; terminating the process
    # removes them, so the unchanged production stale-worker probe detects it.
    code = """
import httpx, sys, time
while True:
    try:
        r = httpx.post(sys.argv[1] + '/api/workers/heartbeat',
            headers={'x-vertep-token': sys.argv[2]},
            json={'node_name': sys.argv[3], 'role': 'text', 'status': 'FREE',
                  'capabilities': ['llm']}, timeout=5)
        r.raise_for_status()
    except httpx.TransportError:
        pass  # CORE itself is restarted by this scenario.
    time.sleep(.4)
"""
    def service():
        return subprocess.Popen([sys.executable, "-c", code, core["url"], TOKEN, NODE], cwd=ROOT)

    worker = service()
    try:
        _wait_until(lambda: any(w["node_name"] == NODE for w in api.get("/api/workers").json()),
                    message="service heartbeat")
        assert not [a for a in api.get("/api/alerts").json() if a.get("node_name") == NODE]
        worker.terminate()
        worker.wait(timeout=10)
        def node_alerts():
            response = api.get("/api/alerts")
            response.raise_for_status()
            return [a for a in response.json() if a.get("node_name") == NODE]
        _wait_until(lambda: any(a["state"] == "firing" for a in node_alerts()),
                    message="failed service probe to firing alert")
        alert = node_alerts()[0]
        evidence = {"firing": alert}
        assert len(node_alerts()) == 1
        page.goto(core["url"] + "/alerts")
        expect(page.get_by_test_id("alerts-page")).to_contain_text(NODE)
        page.get_by_test_id("alert-ack-" + alert["id"]).click()
        _wait_until(lambda: node_alerts()[0]["state"] == "acknowledged", message="Browser acknowledgement")
        evidence["acknowledged"] = node_alerts()[0]
        core["restart"]()
        assert node_alerts()[0]["id"] == alert["id"]
        assert node_alerts()[0]["state"] == "acknowledged"
        evidence["after_restart_acknowledged"] = node_alerts()[0]
        worker = service()
        _wait_until(lambda: node_alerts()[0]["state"] == "resolved", message="actual service recovery")
        assert node_alerts()[0]["id"] == alert["id"]
        core["restart"]()
        assert node_alerts()[0]["state"] == "resolved"
        evidence["after_restart_resolved"] = node_alerts()[0]
        page.reload()
        expect(page.get_by_test_id("alerts-page")).to_contain_text("resolved")
        page.screenshot(path=str(core["root"] / "monitoring-recovery.png"), full_page=True)
        (core["root"] / "monitoring-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=10)


def test_actual_logs_cursor_entity_and_empty(monitoring_core, monitoring_page):
    core, page = monitoring_core, monitoring_page
    # Logging ingestion, pagination and entity hrefs remain real HTTP paths.
    api = core["api"]
    response = api.post("/api/workers/heartbeat", json={"node_name": NODE,
                        "role": "text", "status": "FREE", "capabilities": ["llm"]})
    assert response.status_code == 200, response.text
    response = api.post("/api/logs/ingest", json={"node_name": NODE, "entries": [
        {"level": "INFO", "message": f"cursor-proof-{i:03d}"} for i in range(205)]})
    assert response.status_code == 200, response.text
    page.goto(core["url"] + "/logs")
    page.locator('input[name="nodeName"]').fill(NODE)
    page.get_by_role("button", name="Оновити", exact=True).click()
    expect(page.get_by_test_id("logs-table").locator("tbody tr")).to_have_count(200)
    with page.expect_response(lambda r: "/api/logs?" in r.url and "before=" in r.url) as older:
        page.get_by_test_id("logs-load-older").click()
    assert older.value.ok
    expect(page.get_by_test_id("logs-table").locator("tbody tr")).to_have_count(205)
    page.get_by_test_id("logs-table").get_by_role("link", name=NODE, exact=True).first.click()
    expect(page).to_have_url(core["url"] + "/workers/" + NODE)
    expect(page.get_by_test_id("worker-detail-page").get_by_role("heading", name=NODE, exact=True)).to_be_visible()
    page.goto(core["url"] + "/logs")
    page.locator('input[name="nodeName"]').fill("missing-disposable-node")
    page.get_by_role("button", name="Оновити", exact=True).click()
    expect(page.locator("app-empty-state")).to_be_visible()


@pytest.mark.parametrize("surface", ["logs", "alerts"])
def test_loading_and_network_error(monitoring_core, monitoring_page, surface):
    page = monitoring_page
    pending = []
    # Hold the real request to observe loading, then continue to the real API.
    pattern = "**/api/" + surface + "*"
    view = page.get_by_test_id(surface + "-page")
    page.route(pattern, lambda route: pending.append(route))
    page.goto(monitoring_core["url"] + "/" + surface)
    expect(view.locator("app-loading-state")).to_be_visible()
    for route in list(pending):
        route.continue_()
    page.unroute(pattern)
    expect(view.locator("app-loading-state")).not_to_be_visible()
    # A network outage is the controlled failure; no fabricated API payload.
    page.context.set_offline(True)
    page.get_by_role("button", name="Оновити", exact=True).click()
    expect(view.locator("app-error-state")).to_be_visible()
    page.context.set_offline(False)
