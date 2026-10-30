"""#364: committed enrollment/retry and measured Setup/Fleet Browser acceptance."""

import core.telegram.handlers as _owner_core_telegram_handlers
import base64
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from tests.acceptance_provenance import acceptance_provenance
from tests.test_provider_process_acceptance import config_contour, ROOT, PASSWORD
from tests.test_text_redis_process import port, wait
from tests.test_queue_redis_contract import redis_url
from tests.test_media_browser_e2e import sync_playwright, expect


requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="node enrollment integration requires openssl",
)


@pytest.fixture
def node_dependencies(tmp_path, redis_url, record_property):
    pg_bin = Path(os.getenv("VERTEP_TEST_POSTGRES_BIN", "/usr/lib/postgresql/16/bin"))
    suffix = ".exe" if os.name == "nt" else ""
    if not (pg_bin / ("initdb" + suffix)).is_file():
        if os.getenv("VERTEP_REQUIRE_NODE_PROCESS") == "1":
            pytest.fail("actual disposable PostgreSQL binaries required")
        pytest.skip("actual PostgreSQL is unavailable")
    record_property("postgres_version", subprocess.check_output([str(pg_bin / ("postgres" + suffix)), "--version"], text=True).strip())
    record_property("postgres_binary_sha256", hashlib.sha256((pg_bin / ("postgres" + suffix)).read_bytes()).hexdigest())
    record_property("docker_dependency", "controlled external CLI; no container build claim")
    data = tmp_path / "postgres-data"
    # PostgreSQL otherwise uses /var/run/postgresql for its Unix socket even
    # when every client in this acceptance fixture connects through TCP.  The
    # hosted runner does not grant that directory to the disposable initdb
    # owner, so use a per-run directory rather than a privileged workaround.
    socket_dir = tmp_path / "postgres-socket"
    socket_dir.mkdir(mode=0o700)
    initialized = subprocess.run([str(pg_bin / ("initdb" + suffix)), "-D", str(data), "-U", "vertep", "-A", "trust", "--encoding=UTF8", "--no-locale"], capture_output=True, text=True)
    assert initialized.returncode == 0, initialized.stderr
    pg_port = port()
    log = (tmp_path / "postgres.log").open("w", encoding="utf-8")
    pg = subprocess.Popen([str(pg_bin / ("postgres" + suffix)), "-D", str(data),
                           "-h", "127.0.0.1", "-k", str(socket_dir), "-p", str(pg_port)],
                          stdout=log, stderr=log)
    dsn = f"postgresql://vertep@127.0.0.1:{pg_port}/postgres"
    def postgres_ready():
        import psycopg
        try:
            with psycopg.connect(dsn, connect_timeout=1) as connection:
                return connection.execute("SELECT 1").fetchone() == (1,)
        except psycopg.Error:
            return False
    wait(postgres_ready)
    tools = tmp_path / "external-tools"; tools.mkdir()
    fault = tools / "docker-unavailable"
    external = tools / "docker_protocol.py"
    external.write_text('''import json,sys
from pathlib import Path
args=sys.argv[1:]
root=Path(sys.argv[0]).resolve().parent
with (root/'calls.jsonl').open('a') as out: out.write(json.dumps(args)+'\\n')
if (root/'docker-unavailable').exists(): raise SystemExit(1)
if args and args[0]=='info': print('Controlled Docker dependency'); raise SystemExit(0)
if 'compose' in args and 'ps' in args:
    env=Path(args[args.index('--env-file')+1])
    plan=json.loads((env.parent/'config/deployment-plan.json').read_text())
    for service in plan['services']:
        print(json.dumps({'Service':service,'State':'exited' if service=='migrate' else 'running','Health':'healthy','ExitCode':0,'Image':'controlled:'+service}))
''', encoding="utf-8")
    launcher = tools / ("docker.cmd" if os.name == "nt" else "docker")
    launcher.write_text(f'@echo off\n"{sys.executable}" "{external}" %*\n' if os.name == "nt" else f'#!/bin/sh\nexec "{sys.executable}" "{external}" "$@"\n', encoding="utf-8")
    launcher.chmod(0o700)
    if os.name == "nt":
        from pip._vendor.distlib.scripts import ScriptMaker
        maker = ScriptMaker(None, str(tools))
        maker._write_script({"docker"}, ("#!" + sys.executable + "\n").encode(), external.read_bytes(), [], "py")
    env = {"PATH": str(tools) + os.pathsep + os.environ["PATH"], "DATABASE_URL": dsn,
        "REDIS_URL": redis_url, "OLLAMA_URL": "http://127.0.0.1:1", "NODE_ROLE": "core"}
    try:
        yield {"env": env, "fault": fault, "postgres_ready": postgres_ready, "tools": tools}
    finally:
        pg.terminate(); pg.wait(timeout=15); log.close()


@requires_openssl
def test_stock_enrollment_lost_response_after_commit_renew_revoke(config_contour, tmp_path, monkeypatch):
    c = config_contour; api = c["api"]
    token = api.post("/api/nodes/registration-tokens", json={"role": "voice"}).json()["token"]
    committed = []
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            response = httpx.post(c["url"] + self.path, content=body, headers={"Content-Type": "application/json"}, timeout=30)
            assert response.status_code == 200, response.text
            committed.append(response.json())
            if len(committed) == 1:
                assert len(api.get("/api/nodes").json()) == 1
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(response.content)
    with ThreadingHTTPServer(("127.0.0.1", 0), Proxy) as proxy:
        thread = threading.Thread(target=proxy.serve_forever, daemon=True); thread.start()
        credentials = tmp_path / "enrolled" / "node-credentials.json"
        code = "from worker.service import _retry_enrollment; import httpx,os; c=httpx.Client(); _retry_enrollment(c,os.environ['ENROLL_URL'],'retry-voice',{},['speech_synthesis'])"
        env = {**os.environ, "NODE_CONFIG_PATH": str(credentials), "REGISTRATION_TOKEN": token,
            "ENROLL_URL": f"http://127.0.0.1:{proxy.server_port}", "ENROLLMENT_RETRY_BASE_SECONDS": "0", "ENROLLMENT_RETRY_ATTEMPTS": "3"}
        try:
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=45)
            assert result.returncode == 0, result.stderr
        finally:
            proxy.shutdown(); thread.join(timeout=5)
    stored = json.loads(credentials.read_text())
    assert len(committed) == 2
    nodes = api.get("/api/nodes").json()
    assert len(nodes) == 1 and nodes[0]["node_id"] == "retry-voice"
    assert stored["jwt"] == committed[1]["jwt"] != committed[0]["jwt"]
    assert nodes[0]["credential_generation"] == 2
    assert nodes[0]["capabilities"] == ["speech_synthesis"]
    csr = (credentials.parent / "pki/node.csr").read_text()
    old = {"X-Vertep-Token": committed[0]["jwt"]}
    current = {"X-Vertep-Token": stored["jwt"]}
    assert api.post("/api/nodes/retry-voice/renew", json={"csr": csr}, headers=old).status_code == 401
    renewed = api.post("/api/nodes/retry-voice/renew", json={"csr": csr}, headers=current)
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["jwt"] != stored["jwt"]
    assert api.post("/api/nodes/retry-voice/revoke").status_code == 200
    assert api.post("/api/nodes/retry-voice/renew", json={"csr": csr}, headers={"X-Vertep-Token": renewed.json()["jwt"]}).status_code == 401
    (tmp_path / "enrollment-evidence.json").write_text(json.dumps({"committed_responses": len(committed), "nodes": 1,
        "generations": nodes[0]["credential_generation"], "outbound_only": True, "renew": 200, "old_and_revoked": 401}), encoding="utf-8")


@pytest.mark.parametrize("config_contour", ["fresh"], indirect=True)
def test_setup_browser_deployment_manifest_enrollment_and_reload(config_contour, node_dependencies):
    c = config_contour; api = c["api"]
    node_dependencies["fault"].write_text("controlled dependency failure")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(); page = browser.new_page(); errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(c["url"] + "/setup?token=setup-acceptance-code")
            expect(page.locator("#nodeRole")).to_be_visible()
            page.locator("#nodeRole").select_option("core")
            page.locator("#next").click()
            page.locator("#name").fill("Перевірений CORE")
            page.locator("#next").click()
            page.locator("#username").fill("admin")
            page.locator("#password").fill(PASSWORD)
            page.locator("#confirmation").fill(PASSWORD)
            page.locator("#next").click(); page.locator("#next").click(); page.locator("#next").click()
            page.locator("#backend").select_option("skip")
            page.locator("#next").click()
            expect(page.locator("#health")).to_contain_text("OFFLINE", timeout=15000)
            expect(page.locator("#next")).to_be_disabled()
            node_dependencies["fault"].unlink()
            page.locator("#back").click(); page.locator("#next").click()
            expect(page.locator("#next")).to_be_enabled(timeout=15000)
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/setup/complete")) as completed:
                page.locator("#next").click()
            assert completed.value.status == 200, completed.value.text()
            manifest = completed.value.json()
            request = json.loads((c["config"] / "deployment-request.json").read_text())
            assert request["role"] == "core" and request["ai_backend"] == "skip"
            root = c["root"]
            shutil.copyfile(ROOT / "config/node_roles.json", root / "config/node_roles.json")
            shutil.copyfile(ROOT / "docker-compose.yml", root / "docker-compose.yml")
            (root / ".env").write_text("VERTEP_VERSION=" + (ROOT / "VERSION").read_text().strip() + "\nNODE_ROLE=unassigned\n")
            spec = importlib.util.spec_from_file_location("actual_deployment_acceptance", ROOT / "scripts/apply-deployment.py")
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            environment = {**os.environ, **node_dependencies["env"]}
            def runner(command, **kwargs):
                executable = shutil.which(command[0], path=environment["PATH"])
                return subprocess.run([executable or command[0], *command[1:]], env=environment, **kwargs)
            result = module.apply(root, runner=runner)
            assert result["state"] == "SUCCEEDED" and result["runtime_set_verified"] is True
            public = json.loads((root / "config/installation-manifest.json").read_text())
            assert public["node_role"] == "core" and public["ai_backend"]["type"] == "skip"
            c["restart"]("core")
            measured = api.get("/api/system/roles").json()
            assert measured["role_runtime_status"]["core"] == "READY", measured
            from core.node_registry import create_node_csr
            csr = create_node_csr("setup-gpu", c["worker"] / "pki")
            registered = api.post("/api/nodes/register", json={"registration_token": manifest["registration_token"]["token"],
                "node_id": "setup-gpu", "csr": csr, "capabilities": ["image_generation"], "hardware": {}, "version": "test",
                "enrollment_id": "setup-enrollment-proof"})
            assert registered.status_code == 200, registered.text
            page.goto(c["url"] + "/login")
            # Use the canonical session; the tested setup mutation above remains UI-driven.
            response = page.context.request.post(c["url"] + "/api/session", headers={"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()})
            assert response.status == 200
            page.goto(c["url"] + "/workers")
            page.reload()
            expect(page.get_by_test_id("workers-page")).to_contain_text("setup-gpu")
            assert any(n["node_id"] == "setup-gpu" for n in api.get("/api/nodes").json())
            assert json.loads((c["config"] / "installation.json").read_text())["ai_backend"]["type"] == "skip"
            assert errors == []
            page.screenshot(path=str(c["root"] / "setup-fleet.png"), full_page=True)
        finally:
            browser.close()


@pytest.mark.parametrize("config_contour", ["dependencies"], indirect=True)
def test_measured_seven_role_runtime_api_fleet_and_dispatch(config_contour, node_dependencies, monkeypatch):
    """Actual probes/tasks; only GPU/LLM/TTS/publisher/metrics dependencies are controlled."""
    c = config_contour; api = c["api"]
    for key, value in node_dependencies["env"].items():
        monkeypatch.setenv(key, value)
    from worker.service import role_self_test
    from core.dispatcher import available_worker
    from core.models import Job, JobStatus
    from tests.test_mpt_media_matrix import _png
    image = _png(c["root"] / "gpu-result.png", (60, 120, 200), 64, 64).read_bytes()
    calls = []
    class Runtime(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            calls.append(("GET", self.path))
            self.send_response(200); self.end_headers(); self.wfile.write(b"healthy")
        def do_POST(self):
            calls.append(("POST", self.path))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/gpu":
                payload = image
            elif self.path == "/api/generate":
                script = {"title": "Measured runtime", "description": "", "hashtags": [],
                    "scenes": [{"prompt": "Measured scene", "video_prompt": "Measured video", "voiceover": "", "duration": 4}]}
                payload = json.dumps({"response": json.dumps(script)}).encode()
            elif self.path == "/synthesize":
                import io, wave
                audio = io.BytesIO()
                with wave.open(audio, "wb") as out:
                    out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000); out.writeframes(b"\x00\x00" * 2400)
                payload = json.dumps({"audio_base64": base64.b64encode(audio.getvalue()).decode()}).encode()
            else:
                payload = b'{"configured":true,"publication_id":"measured-publication"}'
            self.send_response(200); self.end_headers(); self.wfile.write(payload)
    with ThreadingHTTPServer(("127.0.0.1", 0), Runtime) as runtime:
        thread = threading.Thread(target=runtime.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{runtime.server_port}"
        for key, value in {"OLLAMA_URL": base, "TTS_HEALTH_URL": base + "/health",
                "PUBLISHER_HEALTH_URL": base + "/health", "PROMETHEUS_URL": base + "/metrics",
                "BACKUP_ROOT": str(c["root"] / "backup-probe"), "DEMO_MODE": "true", "VERTEP_DEMO": "true"}.items():
            monkeypatch.setenv(key, value)
        class Compute:
            def generate_output(self, *args):
                response = httpx.post(base + "/gpu", json={"workflow": "self-test"})
                response.raise_for_status()
                return response.content, ".png", "image"
        try:
            roles = json.loads((ROOT / "config/node_roles.json").read_text())
            outputs = {}
            measured = api.get("/api/system/roles").json()
            assert measured["role_runtime_status"]["core"] == "READY", measured
            outputs["core"] = measured["role_runtime_status"]["core"]
            task_types = {"gpu": "image", "text": "text", "voice": "voice", "publisher": "publish", "backup": "backup", "monitoring": "metrics"}
            credentials = {}
            for role, task_type in task_types.items():
                result = role_self_test(role, {"gpu_available": True}, Compute())
                assert result["status"] == "PASSED" and result["evidence"], (role, result)
                outputs[role] = result
                node = "measured-" + role
                token = api.post("/api/nodes/registration-tokens", json={"role": role}).json()["token"]
                from core.node_registry import create_node_csr
                csr = create_node_csr(node, c["root"] / (node + "-pki"))
                caps = roles[role]["capabilities"]
                registered = api.post("/api/nodes/register", json={"registration_token": token,
                    "node_id": node, "csr": csr, "capabilities": caps, "hardware": {}, "version": "test"})
                assert registered.status_code == 200, registered.text
                headers = {"X-Vertep-Token": registered.json()["jwt"]}
                credentials[role] = headers
                heartbeat = api.post("/api/workers/heartbeat", json={"node_name": node, "role": role,
                    "status": "FREE", "capabilities": caps, "self_test": result, "supported_tasks": [task_type]}, headers=headers)
                assert heartbeat.status_code == 200, heartbeat.text
                state = api.get("/api/system/roles").json()
                assert state["role_runtime_status"][role] == "READY", (role, state)
                worker = next(w for w in api.get("/api/workers").json() if w["node_name"] == node)
                job = Job(job_id="dispatch-" + role, topic="Measured", character_id="measured", task_type=task_type,
                    priority=5, status=JobStatus.NEW, created_at=datetime.now(timezone.utc).isoformat())
                assert available_worker([worker], job) is not None, (role, worker)
            empty_full = api.post("/api/system/test", json={"scope": "full"}).json()
            assert empty_full["result"] == "UNHEALTHY"
            assert "task_results(no_verified_artifacts)" in empty_full["full_test_failures"]
            from tests.test_system_state_mutation_matrix import CHARACTER_BODY
            assert api.post("/api/characters", json=CHARACTER_BODY).status_code == 200
            created = api.post("/api/jobs", json={"topic": "Full self-test output", "character_id": "matrix_char"})
            assert created.status_code == 200, created.text
            job_id = created.json()["job_id"]
            wait(lambda: api.get(f"/api/jobs/{job_id}").json()["status"] == "SCRIPT_QUEUED")
            claimed = api.post("/api/tasks/claim", json={"node_name": "measured-text", "vram_mb": 0,
                "supported_tasks": ["text"]}, headers=credentials["text"])
            assert claimed.status_code == 200, claimed.text
            task = claimed.json()["task"]
            assert task and task["job_id"] == job_id, claimed.text
            assert next(w for w in api.get("/api/workers").json() if w["node_name"] == "measured-text")["status"] == "BUSY"
            from worker.role_executor import execute_role_task
            monkeypatch.setenv("DEMO_MODE", "false")
            artifacts = execute_role_task("text", task)
            assert json.loads(base64.b64decode(artifacts[0]["data_base64"]))["title"] == "Measured runtime"
            submitted = api.post("/api/tasks/result", json={"node_name": "measured-text", "task_id": task["task_id"],
                "job_id": job_id, "success": True, "artifacts": artifacts}, headers=credentials["text"])
            assert submitted.status_code == 200, submitted.text
            wait(lambda: api.get(f"/api/jobs/{job_id}").json()["status"] == "SCRIPT_PENDING_APPROVAL")
            for role in task_types:
                assert api.post("/api/workers/heartbeat", json={"node_name": "measured-" + role, "role": role,
                    "status": "FREE", "capabilities": roles[role]["capabilities"], "self_test": outputs[role]}, headers=credentials[role]).status_code == 200
            full = api.post("/api/system/test", json={"scope": "full"}).json()
            (c["root"] / "full-self-test-evidence.json").write_text(json.dumps(full), encoding="utf-8")
            assert full["result"] == "HEALTHY", full
            assert len(full["runtime_probes"]) == 6
            assert full["certificates"]["execution_probes"][0]["signature_verified"] is True
            assert full["storage"]["execution_probe"] == "write-fsync-delete"
            assert full["task_results"]["verified_artifacts"][0]["job_id"] == job_id
            # Actual confirmed callback -> authenticated HTTP CORE self-test ->
            # persisted operation/result/audit. Only Telegram delivery is replaced.
            import importlib
            core_app = importlib.import_module("core.app")
            from core.telegram.handlers import _sys_test_full_confirm
            from core.operations import list_operations
            from unittest.mock import Mock
            monkeypatch.setenv("CONFIG_ROOT", str(c["config"]))
            monkeypatch.setenv("UPDATE_STATE_DIR", str(c["root"] / "update"))
            monkeypatch.setenv("CORE_API_URL", c["url"])
            monkeypatch.setenv("INTERNAL_API_KEY", "disposable-node-internal-key")
            adapter = Mock(); monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
            _sys_test_full_confirm({"id": "measured-full-confirm"}, "42", "sys_test_full_confirm", "")
            operation = next(op for op in list_operations() if op["type"] == "test")
            assert operation["status"] == "COMPLETED" and operation["result"]["result"] == "HEALTHY", operation
            audit = [json.loads(line) for line in (c["root"] / "update" / (operation["operation_id"] + ".audit.jsonl")).read_text().splitlines()]
            assert audit[-1]["actor"] == "telegram:42" and audit[-1]["chat"] == "42"
            assert audit[-1]["confirmation"] == "sys_test_full_confirm" and audit[-1]["target"] == "full"
            assert audit[-1]["operation_type"] == "test" and audit[-1]["result"]["result"] == "HEALTHY"
            # A fresh heartbeat cannot resurrect stale or revoked attestation.
            stale = dict(outputs["text"], checked_at="2000-01-01T00:00:00+00:00")
            assert api.post("/api/workers/heartbeat", json={"node_name": "measured-text", "role": "text",
                "status": "FREE", "capabilities": roles["text"]["capabilities"], "self_test": stale}, headers=credentials["text"]).status_code == 200
            assert api.get("/api/system/roles").json()["role_runtime_status"]["text"] != "READY"
            assert api.post("/api/nodes/measured-voice/revoke").status_code == 200
            assert api.get("/api/system/roles").json()["role_runtime_status"]["voice"] != "READY"
            assert next(w for w in api.get("/api/workers").json() if w["node_name"] == "measured-voice")["status"] == "REVOKED"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(); context = browser.new_context(base_url=c["url"])
                assert context.request.post("/api/session", headers={"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}).status == 200
                page = context.new_page(); page.goto(c["url"] + "/workers")
                expect(page.get_by_test_id("workers-page")).to_contain_text("measured-monitoring")
                page.reload()
                expect(page.get_by_test_id("workers-page")).to_contain_text("measured-voice")
                page.screenshot(path=str(c["root"] / "measured-fleet.png"), full_page=True)
                browser.close()
            (c["root"] / "role-runtime-evidence.json").write_text(json.dumps({"outputs": outputs, "calls": calls, "full_self_test": full}), encoding="utf-8")
        finally:
            runtime.shutdown(); thread.join(timeout=5)


@pytest.mark.parametrize("config_contour", ["dependencies"], indirect=True)
def test_required_services_and_optional_backend_use_actual_probes(config_contour, node_dependencies):
    c = config_contour; api = c["api"]
    core = api.get("/api/setup/health?role=core").json()
    assert core["ready"] is True, core
    assert core["checks"]["postgres"] == "OK" and core["checks"]["redis"] == "OK"
    assert core["checks"]["ollama"].startswith("OFFLINE")  # optional for CORE/skip
    for role, check in [("text", "ollama"), ("gpu", "comfyui"), ("monitoring", "monitoring")]:
        state = api.get("/api/setup/health", params={"role": role}).json()
        assert state["ready"] is False and state["checks"][check] != "OK", state
    assert api.get("/api/setup/health?role=unassigned").status_code == 422
    node_dependencies["fault"].write_text("unavailable")
    blocked = api.get("/api/setup/health?role=core").json()
    assert blocked["ready"] is False and blocked["checks"]["docker"].startswith("OFFLINE"), blocked
    node_dependencies["fault"].unlink()
    # Open TCP without a database protocol cannot satisfy the SQL check.
    class TCP(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
    with ThreadingHTTPServer(("127.0.0.1", 0), TCP) as wrong_service:
        thread = threading.Thread(target=wrong_service.serve_forever, daemon=True); thread.start()
        try:
            (c["root"] / ".env").write_text("DATABASE_URL=postgresql://vertep@127.0.0.1:" + str(wrong_service.server_port) + "/postgres?connect_timeout=1\n")
            c["restart"]("core")
            invalid = api.get("/api/setup/health?role=core").json()
            assert invalid["ready"] is False and invalid["checks"]["postgres"].startswith("OFFLINE"), invalid
        finally:
            wrong_service.shutdown(); thread.join(timeout=5)


@requires_openssl
def test_fleet_browser_token_enrollment_retry_and_forbidden_mutations(config_contour, monkeypatch):
    c = config_contour; api = c["api"]
    state_file = c["root"] / "update/system-state.json"
    state_file.parent.mkdir(exist_ok=True)
    from tests.test_core_access_browser_e2e import browser_request
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(); context = browser.new_context(base_url=c["url"])
        assert context.request.post("/api/session", headers={"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}).status == 200
        page = context.new_page(); errors = []; console = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: console.append(message.text) if message.type == "error" else None)
        try:
            page.goto(c["url"] + "/workers")
            expect(page.locator("app-header")).to_contain_text("Нормальний", timeout=15000)
            page.get_by_test_id("create-worker-button").click()
            page.get_by_test_id("worker-role-select").select_option("backup")
            # The server becomes READ_ONLY after the form was opened: exercise
            # an actual refusal, then retry the same UI mutation after recovery.
            state_file.write_text('{"state":"READ_ONLY"}', encoding="utf-8")
            with page.expect_response(lambda r: r.url.endswith("/api/nodes/registration-tokens") and r.request.method == "POST") as denied:
                page.get_by_test_id("generate-token-button").click()
            assert denied.value.status == 423
            state_file.write_text('{"state":"NORMAL"}', encoding="utf-8")
            with page.expect_response(lambda r: r.url.endswith("/api/nodes/registration-tokens") and r.request.method == "POST") as generated:
                page.get_by_test_id("generate-token-button").click()
            assert generated.value.status == 200, generated.value.text()
            token = generated.value.json()["token"]
            from core.node_registry import create_node_csr
            csr = create_node_csr("fleet-backup", c["root"] / "fleet-client-pki")
            registered = api.post("/api/nodes/register", json={"registration_token": token, "node_id": "fleet-backup",
                "csr": csr, "capabilities": ["backup"], "hardware": {}, "version": "test"})
            assert registered.status_code == 200, registered.text
            headers = {"X-Vertep-Token": registered.json()["jwt"]}
            from worker.service import role_self_test
            monkeypatch.setenv("BACKUP_ROOT", str(c["root"] / "fleet-backup-store"))
            measured = role_self_test("backup", {})
            assert measured["status"] == "PASSED" and measured["evidence"]["sha256"]
            assert api.post("/api/workers/heartbeat", json={"node_name": "fleet-backup", "role": "backup", "status": "FREE",
                "capabilities": ["backup"], "self_test": measured}, headers=headers).status_code == 200
            page.get_by_role("button", name="Перевірити статус", exact=True).click()
            expect(page.get_by_role("heading", name="Вузол зареєстровано", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name="Закрити", exact=True).click()
            page.reload()
            expect(page.get_by_test_id("workers-page")).to_contain_text("fleet-backup")
            assert next(node for node in api.get("/api/nodes").json() if node["node_id"] == "fleet-backup")["runtime_status"] == "ONLINE"
            for role, mode, status in [("viewer", "NORMAL", 403), ("admin", "READ_ONLY", 423)]:
                state_file.write_text(json.dumps({"state": mode}), encoding="utf-8")
                limited = browser.new_context(base_url=c["url"])
                user, password = ("reader", "config-viewer-password") if role == "viewer" else ("admin", PASSWORD)
                assert limited.request.post("/api/session", headers={"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}).status == 200
                denied_page = limited.new_page(); denied_page.goto(c["url"] + "/workers")
                expect(denied_page.get_by_test_id("create-worker-button")).to_be_disabled()
                before = api.get("/api/nodes").json()
                assert browser_request(denied_page, "POST", "/api/nodes/registration-tokens", {"role": "voice"})["status"] == status
                assert browser_request(denied_page, "POST", "/api/nodes/fleet-backup/actions", {"action": "disable"})["status"] == status
                assert api.get("/api/nodes").json() == before
                limited.close()
            assert errors == []
            assert not any("ReferenceError" in item or "TypeError" in item or "500" in item for item in console), console
            (c["root"] / "fleet-browser-evidence.json").write_text(json.dumps({"pageerrors": errors,
                "console": console, "token_retry": [423, 200], "enrolled": "fleet-backup", "forbidden": [403, 423]}), encoding="utf-8")
        finally:
            context.close(); browser.close()


def test_unhealthy_full_test_cannot_report_callback_success(config_contour, monkeypatch):
    c = config_contour
    assert c["api"].post("/api/system/test", json={"scope": "full"}).json()["result"] == "UNHEALTHY"
    import importlib
    from unittest.mock import Mock
    from core.telegram.handlers import _sys_test_full_confirm
    from core.operations import list_operations
    monkeypatch.setenv("UPDATE_STATE_DIR", str(c["root"] / "update"))
    monkeypatch.setenv("CORE_API_URL", c["url"])
    monkeypatch.setenv("INTERNAL_API_KEY", "disposable-node-internal-key")
    adapter = Mock()
    monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
    _sys_test_full_confirm({"id": "failed-full-confirm"}, "42", "sys_test_full_confirm", "")
    operation = next(op for op in list_operations() if op["type"] == "test")
    assert operation["status"] == "FAILED"
    assert "не пройдено" in adapter.answer_callback.call_args[0][1]
    audit = json.loads((c["root"] / "update" / (operation["operation_id"] + ".audit.jsonl")).read_text().splitlines()[-1])
    assert audit["status"] == "FAILED" and audit["actor"] == "telegram:42"


def test_file_worker_updates_and_reads_are_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from core.repository import FileRepository
    repository = FileRepository(tmp_path)
    def write(index):
        repository.save_worker({"node_name": f"writer-{index}", "status": "READY"})
        assert all("node_name" in worker for worker in repository.load_workers())
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(48)))
    assert {worker["node_name"] for worker in repository.load_workers()} == {f"writer-{index}" for index in range(48)}


def test_gpu_vendor_readiness_requires_successful_command_output(tmp_path, monkeypatch):
    """Controlled external vendor command; real subprocess and runtime probes."""
    script = b'''import os,sys
mode=os.environ.get('MEASURED_GPU_MODE','ready')
if mode=='failed': print('ROCm version unavailable'); raise SystemExit(1)
if mode=='empty': raise SystemExit(0)
print('ROCm 6.3.0' if '--version' in sys.argv else 'Name: gfx1100')
'''
    if os.name == "nt":
        from pip._vendor.distlib.scripts import ScriptMaker
        ScriptMaker(None, str(tmp_path))._write_script({"rocminfo"}, ("#!" + sys.executable + "\n").encode(), script, [], "py")
    else:
        command = tmp_path / "rocminfo"
        command.write_bytes(("#!" + sys.executable + "\n").encode() + script); command.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GPU_VENDOR", "amd")
    monkeypatch.setenv("ROCM_VERSION", "configured-only")
    from core.health_checks import check_gpu, check_cuda
    assert check_gpu()[0] is True and check_cuda()[0] is True
    for mode in ("failed", "empty"):
        monkeypatch.setenv("MEASURED_GPU_MODE", mode)
        assert check_gpu()[0] is not True and check_cuda()[0] is False


def test_certificate_execution_rejects_a_mismatched_private_key(tmp_path, monkeypatch):
    from core.node_registry import _create_installation_ca
    from core.certificates import certificate_execution_probes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    key, certificate = tmp_path / "ca.key", tmp_path / "ca.crt"
    _create_installation_ca(key, certificate)
    monkeypatch.setenv("NODE_CA_KEY_PATH", str(key)); monkeypatch.setenv("NODE_CA_CERT_PATH", str(certificate))
    monkeypatch.setenv("CORE_CERTIFICATE_PATH", str(tmp_path / "absent.crt"))
    assert certificate_execution_probes()[0]["signature_verified"] is True
    original = key.read_bytes()
    try:
        wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key.write_bytes(wrong.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        assert certificate_execution_probes()[0]["signature_verified"] is False
    finally:
        key.write_bytes(original)


def test_completed_task_evidence_uses_persisted_terminal_records(node_dependencies, tmp_path):
    import psycopg
    from core.repository import FileRepository, MemoryRepository, PostgresRepository
    dsn = node_dependencies["env"]["DATABASE_URL"]
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE TABLE task_attempts(task_id text PRIMARY KEY,job_id text,node_name text,task_type text,status text,error text,completed_at timestamptz)")
    for repository in (FileRepository(tmp_path / "ledger"), MemoryRepository(), PostgresRepository(dsn)):
        repository.record_task({"task_id": "complete", "job_id": "job", "task": "script"}, "CLAIMED", "text-node")
        assert repository.completed_tasks() == []
        repository.record_task({"task_id": "complete", "job_id": "job", "task": "script"}, "COMPLETED", "text-node")
        repository.record_task({"task_id": "failed", "job_id": "job", "task": "script"}, "FAILED", "text-node")
        assert repository.completed_tasks() == [{"task_id": "complete", "job_id": "job", "task_type": "script", "node_name": "text-node"}]
