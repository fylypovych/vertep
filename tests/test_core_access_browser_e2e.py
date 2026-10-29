"""#362: real Browser -> HTTP -> CORE mutation matrix, no page.route payloads."""
import base64
import json
import os
import shutil
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import uvicorn

from core.app import app
from core.state import store
from core.system_state import SystemState, set_system_state, get_system_state, _path
from tests.acceptance_provenance import acceptance_provenance
from tests.test_system_state_mutation_matrix import actual_backend, PROBES, ADMIN_PASSWORD, VIEWER_PASSWORD
from tests.test_media_browser_e2e import _free_port, _wait_until, _web_dist, sync_playwright, expect
from tests.test_queue_redis_contract import redis_url, redis_queue, _bind_queue


VIEWS = {
    "characters": ("/characters", "create-character-button"),
    "brands": ("/brands", "create-brand-button"),
    "workflows": ("/workflows", "create-workflow-button"),
    "models": ("/settings?tab=models", "pull-model-button"),
    "channels": ("/brands", "channel-delete"),
}
CELLS = [("viewer", "NORMAL", 403), ("admin", "READ_ONLY", 423),
         ("viewer", "READ_ONLY", 423), ("admin", "RECOVERING", 423), ("viewer", "RECOVERING", 423)]


@pytest.fixture(autouse=True)
def require_recovery_dependency(request):
    if (request.node.name == "test_browser_recovery_health_gate_and_success"
            and os.getenv("VERTEP_REQUIRE_MEDIA_BROWSER_E2E") == "1"
            and not os.getenv("VERTEP_TEST_REDIS_URL") and not shutil.which("redis-server")):
        pytest.fail("mandatory recovery Browser acceptance requires actual Redis")


@pytest.fixture
def access_backend(actual_backend, monkeypatch):
    _web_dist()
    monkeypatch.setenv("WORKFLOWS_ROOT", str(actual_backend["workflows"]))
    # Bound health probes to disposable loopback dependencies, never a host's
    # configured database or an unbounded external DNS lookup.
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.2")
    from core.security import _hash_secret
    users = json.loads(os.environ["USERS_JSON"])
    users["admin"] = {"password_hash": _hash_secret(ADMIN_PASSWORD), "role": "admin"}
    monkeypatch.setenv("USERS_JSON", json.dumps(users))
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    def ready():
        try:
            return httpx.get(url + "/api/session", headers=actual_backend["admin"], timeout=3).status_code == 200
        except httpx.HTTPError:
            return False
    try:
        _wait_until(ready, message="access matrix actual CORE")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            def login(role, base=None):
                base = base or url
                context = browser.new_context(base_url=base)
                user, password = ("admin", ADMIN_PASSWORD) if role == "admin" else ("reader", VIEWER_PASSWORD)
                auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
                response = context.request.post(base + "/api/session", headers={"Authorization": auth})
                assert response.status == 200 and response.json()["role"] == role, response.text()
                page = context.new_page()
                page.set_default_timeout(15000)
                return context, page
            yield {**actual_backend, "url": url, "login": login}
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive()


def browser_request(page, method, path, body=None):
    return page.evaluate("""async ({method,path,body}) => {
      const csrf = document.cookie.split(';').map(x=>x.trim()).find(x=>x.startsWith('vertep_csrf='))?.split('=').slice(1).join('=');
      const headers = {'Content-Type':'application/json'};
      if(csrf) headers['X-CSRF-Token'] = decodeURIComponent(csrf);
      const r = await fetch(path,{method,headers,credentials:'same-origin',body: body===null ? undefined : JSON.stringify(body)});
      return {status:r.status, body:await r.text()};
    }""", {"method": method, "path": path, "body": body})


def resources(ctx):
    result = {}
    for key in ("characters", "brands", "workflows"):
        result[key] = {p.relative_to(ctx[key]).as_posix(): p.read_bytes().hex()
                       for p in ctx[key].rglob("*") if p.is_file()}
    result["channels"] = [c.model_dump(mode="json") for c in store.repository.list_channels()]
    from core.operations import list_operations
    result["model_operations"] = list_operations()
    return result


@pytest.mark.parametrize("role,mode,expected", CELLS)
@pytest.mark.parametrize("resource", sorted(VIEWS))
def test_browser_role_state_resource_matrix(access_backend, tmp_path, role, mode, expected, resource):
    ctx = access_backend
    probe = PROBES[resource](ctx)
    set_system_state(SystemState(mode), "Browser access acceptance")
    context, page = ctx["login"](role)
    try:
        # Settings are admin-only; a viewer still exercises its canonical model
        # endpoint from an authenticated public resource page.
        view = "/brands" if resource == "models" and role == "viewer" else VIEWS[resource][0]
        page.goto(ctx["url"] + view)
        expect(page.get_by_role("button", name="Меню користувача", exact=True)).to_be_visible()
        label = {"NORMAL": "Нормальний", "READ_ONLY": "Тільки читання", "RECOVERING": "Відновлення"}[mode]
        expect(page.locator("app-header")).to_contain_text(label, timeout=15000)
        if resource == "models" and page.get_by_test_id("pull-model-input").count():
            page.get_by_test_id("pull-model-input").fill("matrix-model")
        control = page.get_by_test_id(VIEWS[resource][1])
        if role == "admin" or resource == "channels":
            expect(control.first).to_be_visible()
        if control.count():
            expect(control.first).to_be_disabled()
        else:
            assert role == "viewer", "admin resource view must expose its guarded control"
        before = resources(ctx)
        response = browser_request(page, probe["method"], probe["path"], probe["body"])
        assert response["status"] == expected, response
        assert resources(ctx) == before
        page.screenshot(path=str(tmp_path / "matrix.png"), full_page=True)
        (tmp_path / "matrix.json").write_text(json.dumps({"role": role, "state": mode, "resource": resource,
            "expected_status": expected, "actual_status": response["status"], "mutation": False}), encoding="utf-8")
    finally:
        context.close()


@pytest.mark.parametrize("role", ["admin", "viewer"])
@pytest.mark.parametrize("mode", ["RECOVERING", "READ_ONLY", "EMERGENCY"])
def test_browser_session_profile_csrf_logout_in_restricted_mode(access_backend, role, mode):
    set_system_state(SystemState(mode), "session Browser acceptance")
    context, page = access_backend["login"](role)
    try:
        page.goto(access_backend["url"] + "/profile")
        expect(page.get_by_test_id("profile-page")).to_be_visible()
        page.get_by_test_id("display-name-input").fill("Перевірений користувач")
        with page.expect_response(lambda r: r.url.endswith("/api/session/profile") and r.request.method == "PUT") as saved:
            page.get_by_test_id("account-save").click()
        assert saved.value.status == 200 and saved.value.json()["display_name"] == "Перевірений користувач"
        before = (access_backend["characters"].parent / "config" / "users.json").read_bytes()
        assert json.loads(before)["admin" if role == "admin" else "reader"]["display_name"] == "Перевірений користувач"
        assert json.loads(browser_request(page, "GET", "/api/session")["body"])["display_name"] == "Перевірений користувач"
        rejected = page.evaluate("""async()=>{const r=await fetch('/api/session/profile',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({display_name:'forged'})});return r.status;}""")
        assert rejected == 403
        assert (access_backend["characters"].parent / "config" / "users.json").read_bytes() == before
        assert browser_request(page, "DELETE", "/api/session")["status"] == 200
        session = browser_request(page, "GET", "/api/session")
        assert session["status"] == 401, session
    finally:
        context.close()


def test_unknown_backend_record_is_visible_and_does_not_grant_writes(access_backend):
    set_system_state(SystemState.NORMAL, "unknown precondition")
    _path().write_text('{"state":"FUTURE_UNKNOWN_MODE"}', encoding="utf-8")
    context, page = access_backend["login"]("admin")
    try:
        page.goto(access_backend["url"] + "/brands")
        expect(page.get_by_test_id("create-brand-button")).to_be_disabled()
        expect(page.locator("app-header")).to_contain_text("Аварія", timeout=15000)
        expect(page.locator("app-header")).to_contain_text("Невідомий або пошкоджений")
        before = resources(access_backend)
        result = browser_request(page, "POST", "/api/brands", {"id": "must_not_exist", "name": "Denied"})
        assert result["status"] == 423
        assert resources(access_backend) == before
        assert get_system_state()["state"] == "EMERGENCY"
    finally:
        context.close()


@pytest.mark.parametrize("mode", ["MAINTENANCE", "UPDATING", "RECOVERING"])
def test_browser_create_job_waits_without_dispatch(access_backend, mode):
    ctx = access_backend
    from tests.test_system_state_mutation_matrix import CHARACTER_BODY
    from core.state import task_queue
    response = ctx["client"].post("/api/characters", json=CHARACTER_BODY, headers=ctx["admin"])
    assert response.status_code == 200, response.text
    set_system_state(mode, "Browser waiting acceptance")
    context, page = ctx["login"]("admin")
    try:
        page.goto(ctx["url"] + "/jobs")
        expect(page.get_by_test_id("create-job-button")).to_be_enabled()
        page.get_by_test_id("create-job-button").click()
        modal = page.get_by_test_id("create-job-modal")
        modal.get_by_test_id("job-topic-input").fill("Очікування відновлення системи")
        modal.locator("select").first.select_option("matrix_char")
        before = task_queue.depth()
        with page.expect_response(lambda r: r.url.endswith("/api/jobs") and r.request.method == "POST") as created:
            modal.get_by_role("button", name="Створити", exact=True).click()
        assert created.value.status == 200, created.value.text()
        body = created.value.json()
        assert body["status"] == "WAITING_FOR_SYSTEM"
        persisted = next(job for job in store.repository.load_jobs() if job.job_id == body["job_id"])
        assert persisted.status.value == "WAITING_FOR_SYSTEM"
        assert task_queue.depth() == before
        expect(page.get_by_test_id("jobs-page")).to_contain_text("Очікує систему")
    finally:
        context.close()


@pytest.mark.parametrize("role,mode,expected", [("admin", "READ_ONLY", 423),
    ("admin", "EMERGENCY", 423), ("viewer", "NORMAL", 403)])
def test_browser_create_job_denied_without_persistence(access_backend, role, mode, expected):
    set_system_state(mode, "Browser denied job acceptance")
    context, page = access_backend["login"](role)
    try:
        page.goto(access_backend["url"] + "/jobs")
        expect(page.get_by_test_id("create-job-button")).to_be_disabled()
        before = set(store.jobs)
        persisted = [job.model_dump(mode="json") for job in store.repository.load_jobs()]
        result = browser_request(page, "POST", "/api/jobs", {"topic": "Denied", "character_id": "matrix_char"})
        assert result["status"] == expected, result
        assert set(store.jobs) == before
        assert [job.model_dump(mode="json") for job in store.repository.load_jobs()] == persisted
    finally:
        context.close()


@pytest.mark.parametrize("role,mode,expected", [("admin", "RECOVERING", 423),
    ("admin", "READ_ONLY", 423), ("viewer", "EMERGENCY", 403)])
def test_browser_recovery_denied_preserves_state(access_backend, role, mode, expected):
    set_system_state(mode, "Browser recovery forbidden acceptance")
    context, page = access_backend["login"](role)
    try:
        page.goto(access_backend["url"] + ("/settings?tab=update" if role == "admin" else "/brands"))
        if role == "admin":
            expect(page.get_by_test_id("update-recover-normal")).to_be_disabled()
        before = _path().read_bytes()
        result = browser_request(page, "POST", "/api/system/recovery/normal", {})
        assert result["status"] == expected, result
        assert _path().read_bytes() == before
    finally:
        context.close()


def test_browser_recovery_health_gate_and_success(access_backend, redis_queue, monkeypatch):
    """Real Redis; controlled TCP dependency exercises the existing Postgres health contract.

    This health probe only opens a TCP connection: it is not SQL persistence evidence.
    """
    _bind_queue(monkeypatch, redis_queue)
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.2")
    set_system_state("EMERGENCY", "Browser recovery health acceptance")
    context, page = access_backend["login"]("admin")
    try:
        page.goto(access_backend["url"] + "/settings?tab=update")
        expect(page.get_by_test_id("update-recover-normal")).to_be_enabled()
        before = _path().read_bytes()
        with page.expect_response(lambda r: r.url.endswith("/api/system/recovery/normal")) as failed:
            page.get_by_test_id("update-recover-normal").click()
        assert failed.value.status == 409 and "postgres" in failed.value.text()
        assert _path().read_bytes() == before
        expect(page.get_by_test_id("settings-update")).to_contain_text("Recovery health checks failed")
        class Ready(socketserver.BaseRequestHandler):
            def handle(self):
                pass
        with socketserver.ThreadingTCPServer(("127.0.0.2", 5432), Ready) as dependency:
            thread = threading.Thread(target=dependency.serve_forever, daemon=True)
            thread.start()
            try:
                page.reload()
                expect(page.get_by_test_id("update-recover-normal")).to_be_enabled()
                with page.expect_response(lambda r: r.url.endswith("/api/system/recovery/normal")) as recovered:
                    page.get_by_test_id("update-recover-normal").click()
                assert recovered.value.status == 200, recovered.value.text()
                assert get_system_state()["state"] == "NORMAL"
                page.goto(access_backend["url"] + "/brands")
                expect(page.get_by_test_id("create-brand-button")).to_be_enabled()
                result = browser_request(page, "POST", "/api/brands", {"id": "recovered", "name": "Відновлений"})
                assert result["status"] == 200, result
                assert (access_backend["brands"] / "recovered").exists()
            finally:
                dependency.shutdown()
                thread.join(timeout=5)
    finally:
        context.close()


@pytest.mark.parametrize("fault", ["loading", "error"])
def test_browser_status_transport_fault_and_recovery(access_backend, fault, tmp_path):
    """Real reverse proxy delays or rejects transport; it never fabricates CORE JSON."""
    release = threading.Event()
    intercepted = threading.Event()
    mutations = []
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if self.path == "/api/status" and not release.is_set():
                intercepted.set()
                if fault == "error":
                    self.send_error(503, "Controlled upstream outage")
                    return
                if not release.wait(30):
                    self.send_error(504)
                    return
            self.forward()
        def do_POST(self):
            mutations.append(self.path)
            self.forward()
        def forward(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "connection"}}
            with httpx.Client() as client:
                response = client.request(self.command, access_backend["url"] + self.path,
                    content=body, headers=headers, timeout=20)
            self.send_response(response.status_code)
            for key, value in response.headers.multi_items():
                if key.lower() not in {"transfer-encoding", "content-encoding", "content-length", "connection"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            try:
                self.wfile.write(response.content)
            except (BrokenPipeError, ConnectionResetError):
                pass
    with ThreadingHTTPServer(("127.0.0.1", 0), Proxy) as proxy:
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{proxy.server_port}"
        context, page = access_backend["login"]("admin", base)
        try:
            before = resources(access_backend)
            page.goto(base + "/brands", wait_until="domcontentloaded")
            assert intercepted.wait(10)
            expect(page.get_by_test_id("create-brand-button")).to_be_disabled()
            expect(page.locator("app-header")).to_contain_text("Невизначений" if fault == "loading" else "Недоступний")
            assert mutations == ["/api/session"]
            assert resources(access_backend) == before
            page.screenshot(path=str(tmp_path / f"status-{fault}.png"), full_page=True)
            release.set()
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_test_id("create-brand-button")).to_be_enabled()
            expect(page.locator("app-header")).to_contain_text("Нормальний")
            result = browser_request(page, "POST", "/api/brands", {"id": "transport_recovered", "name": "Відновлений зв’язок"})
            assert result["status"] == 200, result
            assert (access_backend["brands"] / "transport_recovered").exists()
        finally:
            release.set()
            context.close()
            proxy.shutdown()
            thread.join(timeout=5)
