"""#363: actual CORE/executor restarts, persisted rollback, Settings Browser."""
import base64
import json
import hashlib
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from tests.acceptance_provenance import acceptance_provenance
from tests.test_text_redis_process import port, wait
from tests.test_provider_switch import _ready_report
from tests.test_mpt_media_matrix import PinnedRuntime, _approved_assets, _stream_info
from tests.test_media_browser_e2e import _web_dist, sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "config-acceptance-password"
CORE_SECRET = "disposable-core-engine-secret"
WORKER_SECRET = "disposable-worker-engine-secret"


@pytest.fixture
def config_contour(tmp_path, request, record_property):
    record_property("executor_probe_sha256", hashlib.sha256((ROOT / "tests/config_process_probe.py").read_bytes()).hexdigest())
    record_property("contour_sha256", hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    _web_dist()
    runtime = PinnedRuntime(tmp_path)
    requests = []
    class Runtime(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.answer()
        def do_POST(self):
            self.answer()
        def do_DELETE(self):
            self.answer()
        def answer(self):
            requests.append((self.command, self.path))
            if getattr(self.server, "outage", False):
                self.send_error(503)
                return
            token = self.headers.get("Authorization", "")
            if self.path == "/health":
                if getattr(self.server, "reject_worker", False) and token == "Bearer " + WORKER_SECRET:
                    self.send_error(503)
                    return
                response = httpx.Response(200, json=_ready_report())
            elif self.path == "/synthesize":
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                import io, wave
                wav = io.BytesIO()
                with wave.open(wav, "wb") as output:
                    output.setnchannels(1); output.setsampwidth(2); output.setframerate(24000)
                    output.writeframes(b"\x00\x00" * 2400)
                response = httpx.Response(200, json={"audio_base64": base64.b64encode(wav.getvalue()).decode(),
                    "engine": body.get("provider"), "voice": body.get("voice"), "mime_type": "audio/wav"})
            else:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                kwargs = {"headers": dict(self.headers), "content": body}
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    kwargs["json"] = json.loads(body)
                response = runtime._answer(self.command, "http://engine:8000" + self.path, kwargs)
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() != "content-length":
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Runtime) for _ in range(2)]
    servers[1].reject_worker = True
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint, failing_endpoint = [f"http://127.0.0.1:{server.server_port}" for server in servers]
    config, worker = tmp_path / "config", tmp_path / "worker"
    config.mkdir(); worker.mkdir()
    fresh = getattr(request, "param", None) == "fresh"
    if not fresh:
        (config / "installation.json").write_text('{"completed_at":"2026-01-01"}', encoding="utf-8")
        from core.deployment_plan import create_plan
        roles = json.loads((ROOT / "config/node_roles.json").read_text())
        (config / "deployment-plan.json").write_text(json.dumps(create_plan(roles, "core", (ROOT / "VERSION").read_text().strip(), ["gpu", "voice"])))
    (config / "users.json").write_text("{}", encoding="utf-8")
    core_url = f"http://127.0.0.1:{port()}"
    env = {**os.environ, "CONFIG_ROOT": str(config), "JOB_ROOT": str(tmp_path / "core-jobs"),
        "CHARACTERS_ROOT": str(tmp_path / "characters"), "BRANDS_ROOT": str(tmp_path / "brands"),
        "WORKFLOWS_ROOT": str(tmp_path / "workflows"), "UPDATE_STATE_DIR": str(tmp_path / "update"),
        "ADMIN_USER": "admin", "ADMIN_PASSWORD": PASSWORD, "USERS_JSON": "{}",
        "NODE_API_TOKEN": "config-node-token", "REDIS_URL": "redis://127.0.0.1:1/15",
        "INTERNAL_API_KEY": "disposable-node-internal-key",
        "DATABASE_URL": "", "SYSTEM_STATE_BACKEND": "file", "STATE_BACKEND": "file",
        "VERTEP_VIDEO_ENGINE": "native", "TTS_PROVIDER": "none", "TTS_URL": endpoint,
        "PIPER_URL": endpoint, "POSTGRES_HOST": "127.0.0.2", "PROVIDER_ACTIVATION_TIMEOUT_SECONDS": "8",
        "LOCAL_WORKER_FALLBACK": "false", "RATE_LIMIT_PER_MINUTE": "10000"}
    from core.security import _hash_secret
    env["USERS_JSON"] = json.dumps({"reader": {"password_hash": _hash_secret("config-viewer-password"), "role": "viewer"}})
    if fresh or getattr(request, "param", None) == "dependencies":
        env.update(request.getfixturevalue("node_dependencies")["env"])
    if fresh:
        env.update({"NODE_ROLE": "unassigned", "SETUP_TOKEN_HASH": hashlib.sha256(b"setup-acceptance-code").hexdigest(),
            "VERTEP_VERSION": (ROOT / "VERSION").read_text().strip()})
    env.pop("MONEY_PRINTER_TOKEN", None)
    env.pop("MONEY_PRINTER_URL", None)
    worker_env = {**env, "CONFIG_ROOT": str(worker / "config"), "JOB_ROOT": str(worker / "jobs"),
        "CORE_URL": core_url, "PROBE_ROOT": str(worker), "MONEY_PRINTER_TOKEN": WORKER_SECRET,
        "NODE_ROLE": "gpu", "NODE_CAPABILITIES": "speech_synthesis,video_generation"}
    worker_env.pop("PIPER_URL", None)
    processes, logs = {}, []
    api = httpx.Client(base_url=core_url, auth=("admin", PASSWORD), timeout=30)
    def start(name):
        if name == "core" and (tmp_path / ".env").is_file():
            for row in (tmp_path / ".env").read_text().splitlines():
                key, separator, value = row.partition("=")
                if separator:
                    env[key] = value
        log = (tmp_path / f"{name}-{uuid.uuid4().hex}.log").open("w", encoding="utf-8")
        logs.append(log)
        command = [sys.executable, "-m", "uvicorn", "core.app:app", "--host", "127.0.0.1", "--port", core_url.rsplit(":", 1)[1]] if name == "core" else [sys.executable, "-m", "tests.config_process_probe"]
        processes[name] = subprocess.Popen(command, cwd=ROOT, env=env if name == "core" else worker_env, stdout=log, stderr=log)
        def ready():
            try:
                if name == "core":
                    return api.get("/api/setup" if fresh else "/api/session", headers={"X-Vertep-Setup-Token": "setup-acceptance-code"}).status_code == 200
                return observation().get("pid") == processes[name].pid
            except (httpx.HTTPError, ValueError, OSError):
                return False
        wait(ready)
    def stop(name):
        process = processes.get(name)
        if process and process.poll() is None:
            process.terminate(); process.wait(timeout=15)
    def restart(name):
        old = processes[name].pid
        stop(name); start(name)
        assert processes[name].pid != old
    def observation():
        try:
            return json.loads((worker / "observation.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    def task(kind, **payload):
        identifier = uuid.uuid4().hex
        temp = worker / "command.tmp"
        temp.write_text(json.dumps({"id": identifier, "type": kind, **payload}), encoding="utf-8")
        temp.replace(worker / "command.json")
        result = worker / f"{identifier}.json"
        wait(lambda: result.is_file(), timeout=90)
        return json.loads(result.read_text(encoding="utf-8"))
    try:
        start("core")
        if not fresh and getattr(request, "param", None) != "no-worker":
            start("worker")
            wait(lambda: api.get("/api/settings/providers").json().get("worker_acks", {}).get("config-executor", {}).get("VERTEP_VIDEO_ENGINE") == "native")
        yield {"api": api, "url": core_url, "config": config, "root": tmp_path, "worker": worker,
            "endpoint": endpoint, "failing_endpoint": failing_endpoint, "observation": observation,
            "set_outage": lambda active: setattr(servers[0], "outage", active),
            "restart": restart, "start": start, "stop": stop, "task": task, "requests": requests, "runtime": runtime}
    finally:
        stop("worker"); stop("core"); api.close()
        for server in servers:
            server.shutdown(); server.server_close()
        for log in logs:
            log.close()


def test_supported_switch_task_restart_and_failed_activation_rollback(config_contour):
    c = config_contour; api = c["api"]
    response = api.post("/api/settings/providers/tts", json={"backend": "mock"})
    assert response.status_code == 200 and response.json()["worker_activation"]["status"] == "VERIFIED", response.text
    task = {"task": "voice", "topic": "Перевірка конфігурації", "voice": "uk", "language": "uk"}
    assert c["task"]("voice", task=task)["contract"]["provider"] == "mock"
    before = json.loads((c["config"] / "provider-overrides.json").read_text())["overrides"]
    c["restart"]("core"); c["restart"]("worker")
    wait(lambda: c["observation"]().get("observed", {}).get("TTS_PROVIDER") == "mock")
    assert c["task"]("voice", task=task)["contract"]["provider"] == "mock"
    failed = api.post("/api/settings/providers/tts", json={"backend": "piper"})
    assert failed.status_code == 409, failed.text
    assert json.loads((c["config"] / "provider-overrides.json").read_text())["overrides"] == before
    assert api.get("/api/settings/providers").json()["matrix"]["tts"]["backend"] == "mock"
    assert c["observation"]()["observed"]["TTS_PROVIDER"] == "mock"
    assert c["task"]("voice", task=task)["contract"]["provider"] == "mock"


def test_actual_settings_mpt_task_restart_rollback_and_native(config_contour):
    c = config_contour; api = c["api"]
    assets = _approved_assets(c["worker"])
    inputs = {k: [str(x) for x in v] if isinstance(v, list) and v and isinstance(v[0], Path)
        else str(v) if isinstance(v, Path) else v for k, v in assets.items()}
    def current_snapshot():
        from adapters.providers.base import BRIDGE_SCHEMA_VERSION
        config = api.get("/api/settings/video-engine").json()
        snapshot = {"engine_id": config["effective"], "config_revision": config["config_revision"], "bridge_schema_version": BRIDGE_SCHEMA_VERSION}
        if config["effective"] != "native":
            snapshot.update({"secret_reference": config["secret"], "upstream_reference": config["upstream_reference"],
                "endpoint_reference": {"env": "MONEY_PRINTER_URL", "endpoint": config["endpoint"], "configured": True}})
        return snapshot
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(base_url=c["url"])
        response = context.request.post("/api/session", headers={"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()})
        assert response.status == 200
        page = context.new_page(); errors = []; console = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: console.append(message.text) if message.type == "error" else None)
        try:
            page.goto(c["url"] + "/settings?tab=secrets")
            row = page.get_by_test_id("settings-secrets").locator("div.border-b").filter(has_text="money_printer_token")
            row.get_by_role("button", name="Редагувати", exact=True).click()
            page.locator("#secret-money_printer_token").fill(CORE_SECRET)
            with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/settings/secrets/money_printer_token")) as secret_saved:
                row.get_by_role("button", name="Зберегти", exact=True).click()
            assert secret_saved.value.status == 200
            expect(row).to_contain_text("Збережено")
            assert CORE_SECRET not in page.content()
            page.goto(c["url"] + "/settings?tab=video-engine")
            expect(page.get_by_test_id("video-engine-effective")).to_have_text("Vertep Native")
            def apply(endpoint):
                page.get_by_test_id("video-engine-select").select_option("money-printer")
                page.get_by_test_id("video-engine-endpoint").fill(endpoint)
                with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/settings/providers/video_engine"), timeout=30000) as saved:
                    page.get_by_test_id("video-engine-apply").click()
                return saved.value
            saved = apply(c["endpoint"])
            assert saved.status == 200, saved.text()
            assert saved.json()["worker_activation"]["status"] == "VERIFIED"
            expect(page.get_by_test_id("video-engine-effective")).to_have_text("MoneyPrinterTurbo")
            snapshot = current_snapshot()
            assert snapshot["secret_reference"]["source"] == "secret-store"
            rendered = c["task"]("render", inputs=inputs, snapshot=snapshot)
            assert rendered.get("engine") == "money-printer", rendered
            assert _stream_info(Path(rendered["output"]))[2] > 0
            revision = api.get("/api/settings/video-engine").json()["config_revision"]
            before = json.loads((c["config"] / "provider-overrides.json").read_text())["overrides"]
            c["restart"]("core"); c["restart"]("worker")
            page.reload()
            expect(page.get_by_test_id("video-engine-effective")).to_have_text("MoneyPrinterTurbo")
            wait(lambda: c["observation"]().get("engine") == "money-printer")
            assert api.get("/api/settings/video-engine").json()["config_revision"] == revision
            assert c["task"]("render", inputs=inputs, snapshot=snapshot).get("engine") == "money-printer"
            c["runtime"].corrupt_download = b"controlled corrupt media"
            refused = c["task"]("render", inputs=inputs, snapshot=snapshot)
            assert "error" in refused, refused
            assert c["observation"]()["engine"] == "money-printer"
            c["runtime"].corrupt_download = None
            c["set_outage"](True)
            unavailable = c["task"]("render", inputs=inputs, snapshot=snapshot)
            assert "error" in unavailable, unavailable
            assert c["observation"]()["engine"] == "money-printer"
            assert api.get("/api/settings/video-engine").json()["selected"] == "money-printer"
            c["set_outage"](False)
            failed = apply(c["failing_endpoint"])
            assert failed.status == 409, failed.text()
            expect(page.get_by_test_id("video-engine-rollback")).to_be_visible()
            assert json.loads((c["config"] / "provider-overrides.json").read_text())["overrides"] == before
            assert c["observation"]()["observed"]["MONEY_PRINTER_URL"] == c["endpoint"]
            assert c["task"]("render", inputs=inputs, snapshot=snapshot).get("engine") == "money-printer"
            page.get_by_test_id("video-engine-return-native").click()
            expect(page.get_by_test_id("video-engine-effective")).to_have_text("Vertep Native", timeout=30000)
            assert c["task"]("render", inputs=inputs, snapshot=current_snapshot()).get("engine") == "native"
            assert CORE_SECRET not in page.content() and WORKER_SECRET not in page.content()
            for route in ("/api/settings/providers", "/api/settings/video-engine", "/api/jobs"):
                assert CORE_SECRET not in api.get(route).text
            assert all(CORE_SECRET not in log.read_text(encoding="utf-8", errors="replace")
                and WORKER_SECRET not in log.read_text(encoding="utf-8", errors="replace") for log in c["root"].glob("*.log"))
            assert errors == []
            assert not any("ReferenceError" in message or "TypeError" in message or "500" in message for message in console), console
            (c["root"] / "mpt-browser-evidence.json").write_text(json.dumps({"pageerrors": errors, "console": console,
                "persisted_rollback": True, "native_return": True, "revision": revision}), encoding="utf-8")
            page.screenshot(path=str(c["root"] / "mpt-settings.png"), full_page=True)
        finally:
            context.close(); browser.close()


@pytest.mark.parametrize("config_contour", ["no-worker"], indirect=True)
def test_deferred_activation_is_not_success_and_late_failure_rolls_back(config_contour):
    c = config_contour; api = c["api"]
    response = api.post("/api/settings/providers/tts", json={"backend": "piper"})
    assert response.status_code == 202, response.text
    assert response.json()["worker_activation"]["status"] == "AWAITING_WORKER"
    assert api.get("/api/settings/providers").json()["worker_activation"]["tts"]["ok"] is False
    c["start"]("worker")
    wait(lambda: api.get("/api/settings/providers").json()["activation"]["status"] == "ROLLED_BACK")
    assert json.loads((c["config"] / "provider-overrides.json").read_text())["overrides"] == {}
    assert api.get("/api/settings/providers").json()["matrix"]["tts"]["backend"] == "none"
    wait(lambda: c["observation"]().get("observed", {}).get("TTS_PROVIDER") == "none")


@pytest.mark.parametrize("role,mode,status", [("viewer", "NORMAL", 403), ("admin", "READ_ONLY", 423), ("admin", "RECOVERING", 423)])
def test_actual_settings_permissions_and_state_preserve_selection(config_contour, role, mode, status):
    c = config_contour
    if mode != "NORMAL":
        (c["root"] / "update").mkdir(exist_ok=True)
        (c["root"] / "update/system-state.json").write_text(json.dumps({"state": mode}), encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(); context = browser.new_context(base_url=c["url"])
        user, password = ("admin", PASSWORD) if role == "admin" else ("reader", "config-viewer-password")
        response = context.request.post("/api/session", headers={"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()})
        assert response.status == 200
        page = context.new_page()
        try:
            page.goto(c["url"] + ("/settings?tab=video-engine" if role == "admin" else "/brands"))
            if role == "admin":
                expect(page.get_by_test_id("video-engine-select")).to_be_disabled()
            from tests.test_core_access_browser_e2e import browser_request
            before = (c["config"] / "provider-overrides.json").read_bytes() if (c["config"] / "provider-overrides.json").exists() else None
            result = browser_request(page, "POST", "/api/settings/providers/video_engine", {"backend": "money-printer", "endpoint": c["endpoint"]})
            assert result["status"] == status, result
            after = (c["config"] / "provider-overrides.json").read_bytes() if (c["config"] / "provider-overrides.json").exists() else None
            assert before == after
        finally:
            context.close(); browser.close()
