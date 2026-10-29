"""Real-backend Browser+CLI acceptance for the Lab Angular panel (Issue #251).

The panel is served as the production Angular build, the backend is the real
``lab.api`` FastAPI app with a disposable state directory, and the CLI talks to
the very same state.  A tiny in-test static/proxy server mirrors the nginx
contract (SPA fallback + ``/api/lab/`` proxy) without Docker, so the browser
scenarios are reproducible locally and in CI.  Docker packaging has its own
proof in ``tests/test_lab_web_docker.py``.

Criteria covered (automated scope of #251):
* U1 - separate Angular application, own server/process, no production CORE.
* U2 - dashboard/issues/evidence, ir packages, grants/rights/consolidation,
  GPU and decision history read the same backend API/policy/audit as the CLI;
  dangerous actions need explicit confirmation plus a server rights check.
* U3 - Web and CLI agree on state/decisions and on Web/Telegram importance;
  combined configurable reports are identical; an unauthorized client is
  refused regardless of interface; a panel outage does not stop background
  work or protection.
"""

import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest
from tests.lab_evidence_fixture import signed_contract_evidence
from tests.test_lab_github_transport import _GitHub

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "deploy/lab/web/app"
DIST = APP / "dist/vertep-lab-web/browser"
OWNER_TOKEN = "lab-e2e-owner-secret"
API_PORT_ENV = "LAB_E2E_API_PORT"
REQUIRE = os.getenv("VERTEP_REQUIRE_LAB_WEB_E2E") == "1"

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - dependency is required in CI
    if REQUIRE:
        pytest.fail("playwright is not installed; the Lab panel Browser proof is required",
                    pytrace=False)
    pytest.skip("playwright is not installed", allow_module_level=True)


def _require_npm_or_fail():
    npm = shutil.which("npm")
    if npm is None:
        message = "npm is unavailable: the Angular build cannot be produced"
        if os.getenv("VERTEP_REQUIRE_LAB_WEB_E2E") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return npm


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until(predicate, timeout=60, interval=0.1, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    pytest.fail(f"Timed out waiting for {message}")


@pytest.fixture(scope="module")
def angular_build():
    npm = _require_npm_or_fail()
    if (DIST / "index.html").exists():
        return DIST
    if not (APP / "node_modules").exists():
        subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=APP,
                       check=True, timeout=1200)
    subprocess.run([npm, "run", "build"], cwd=APP, check=True, timeout=1200)
    assert (DIST / "index.html").exists(), "Angular build produced no index.html"
    return DIST


class _StaticProxy(BaseHTTPRequestHandler):
    """Serve the Angular build and proxy ``/api/lab/`` to the real API."""

    server_version = "VertepLabWebE2E/1"

    def log_message(self, *_args):  # keep pytest output clean
        pass

    def _send(self, status: int, body: bytes, headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError):
                # The browser navigated away and closed the socket.
                pass

    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        connection = http.client.HTTPConnection(
            self.server.api_host, self.server.api_port, timeout=30)
        forwarded = {key: value for key, value in self.headers.items()
                     if key.lower() not in {"host", "connection"}}
        connection.request(self.command, self.path, body=body, headers=forwarded)
        response = connection.getresponse()
        data = response.read()
        headers = {key: value for key, value in response.getheaders()
                   if key.lower() not in {"transfer-encoding", "connection",
                                          "content-length"}}
        connection.close()
        self._send(response.status, data, headers)

    def _static(self):
        path = urlsplit(self.path).path
        if not path.startswith("/api/lab/"):
            if path == "/" or path.endswith("/"):
                path += "index.html"
            target = (self.server.dist / path.lstrip("/")).resolve()
            if self.server.dist not in target.parents and target != self.server.dist:
                self._send(404, b"not found")
                return
            if target.is_file() and target.name != "index.html":
                self._send(200, target.read_bytes(),
                           {"Content-Type": self._content_type(target),
                            "X-Content-Type-Options": "nosniff"})
                return
        if path.startswith("/api/"):
            self._send(404, b"not found")
            return
        index = self.server.dist / "index.html"
        self._send(200 if index.is_file() else 503, index.read_bytes()
                   if index.is_file() else b"no build",
                   {"Content-Type": "text/html; charset=utf-8",
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff"})

    @staticmethod
    def _content_type(path: Path) -> str:
        return {
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
            ".html": "text/html; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(path.suffix.lower(), "application/octet-stream")

    def do_GET(self):
        if self.path.startswith("/api/lab/"):
            self._proxy()
        else:
            self._static()

    do_HEAD = do_GET
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy


class Lab:
    """One disposable lab environment: API process, panel server, CLI access."""

    def __init__(self, tmp_path: Path, dist: Path):
        self.tmp_path = tmp_path
        self.state = tmp_path / "state"
        self.workspace = tmp_path / "workspace"
        self.state.mkdir()
        self.workspace.mkdir()
        self.api_port = _free_port()
        self.github = _GitHub()
        self.env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "LAB_STATE_DIR": str(self.state),
            "LAB_WORKSPACE": str(self.workspace),
            "LAB_POLICY_GATE_ACTIVE": "true",
            "LAB_OWNER_TOKEN": OWNER_TOKEN,
            "LAB_OWNER_IDENTITY": "owner",
            "LAB_IDLE_SHARING_ENABLED": "false",
            "LAB_GITHUB_API_URL": self.github.url,
            "LAB_GITHUB_TOKEN": "controlled-browser-token",
            "LAB_GITHUB_REPOSITORY": "owner/repo",
            "LAB_EVIDENCE_ROOT": str(tmp_path / "controlled-evidence"),
            "LAB_REVIEW_TRUST_ROOT": str(tmp_path / "controlled-review-trust"),
            API_PORT_ENV: str(self.api_port),
        }
        runner = tmp_path / "lab_web_e2e_app.py"
        runner.write_text(
            "import os\n"
            "import uvicorn\n"
            "from lab.api import create_app\n"
            "from lab.gpu_lease import FakeResourceProvider\n"
            "app = create_app(provider=FakeResourceProvider())\n"
            "uvicorn.run(app, host='127.0.0.1', port=int(os.environ['"
            + API_PORT_ENV + "']), log_level='warning')\n",
            encoding="utf-8")
        self.api_log = open(tmp_path / "api.log", "w", encoding="utf-8")
        self.api = subprocess.Popen(
            [sys.executable, str(runner)], cwd=ROOT, env=self.env,
            stdout=self.api_log, stderr=subprocess.STDOUT)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.api_port}",
                                   trust_env=False, timeout=20)
        _wait_until(lambda: self._api_up(), message="lab API")
        self.web = ThreadingHTTPServer(("127.0.0.1", 0), _StaticProxy)
        self.web.dist = dist
        self.web.api_host = "127.0.0.1"
        self.web.api_port = self.api_port
        self.web_thread = threading.Thread(target=self.web.serve_forever, daemon=True)
        self.web_thread.start()
        self.base_url = f"http://127.0.0.1:{self.web.server_address[1]}"

    def _api_up(self) -> bool:
        if self.api.poll() is not None:
            self.api_log.flush()
            pytest.fail("lab API exited during startup: "
                        + (self.tmp_path / "api.log").read_text(encoding="utf-8"))
        try:
            return self.client.get("/api/lab/status").status_code == 200
        except httpx.HTTPError:
            return False

    def cli(self, *args, check=True) -> dict:
        result = subprocess.run(
            [sys.executable, "-m", "lab.cli", "--state-dir", str(self.state),
             "--workspace", str(self.workspace), *args],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=120)
        if check and not result.stdout.strip():
            raise AssertionError(
                f"CLI {args} produced no JSON: rc={result.returncode} "
                f"stderr={result.stderr}")
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
        payload["__rc"] = result.returncode
        return payload

    def stop_web(self):
        self.web.shutdown()
        self.web.server_close()
        self.web_thread.join(timeout=5)

    def close(self):
        try:
            self.client.close()
        finally:
            if self.api.poll() is None:
                self.api.terminate()
                try:
                    self.api.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.api.kill()
            self.api_log.close()
            self.github.close()
            try:
                self.web.shutdown()
            except Exception:
                pass


@pytest.fixture
def lab(tmp_path, angular_build):
    environment = Lab(tmp_path, angular_build)
    try:
        yield environment
    finally:
        environment.close()


def seed(environment: Lab) -> dict:
    """Create disposable, real backend state through the CLI and HTTP API."""
    token = ["--owner-token", OWNER_TOKEN]
    grant_source = environment.cli(
        "grant", "create_ir_issue", "--subject", "ir.0.0.9.1", *token)
    assert grant_source["granted"] is True
    source = environment.cli(
        "create-issue", "ir", "Browser acceptance candidate",
        "--identifier", "ir.0.0.9.1")
    assert source["created"] is True
    assert environment.cli(
        "grant", "create_ir_issue", "--subject", "ir.0.0.9.2", *token)["granted"]
    revocable = environment.cli(
        "grant", "create_ir_issue", "--subject", "ir.0.0.9.3", *token)
    assert revocable["granted"] is True
    environment.cli(
        "rights-request", "consolidate_ir", "--subject", "ir.0.0.9.1",
        "--reason", "потрібне перенесення залишку на реальному стенді")
    evidence = {
        "identifier": "ir.0.0.9.1",
        "version": "0.0.2.51",
        "commit": "abc1234",
        "environment": "disposable-lab-vm",
        "expected": "усі критерії виконані",
        "actual": "усі критерії виконані",
        "reviewer": "independent-reviewer",
        "test_results": {"passed": 5, "failed": 0, "skipped": 0},
    }
    evidence = signed_contract_evidence(evidence,
        root=environment.env["LAB_EVIDENCE_ROOT"], trust=environment.env["LAB_REVIEW_TRUST_ROOT"])
    verdict = environment.client.post(
        "/api/lab/ir/evidence",
        json={"identifier": "ir.0.0.9.1", "evidence": evidence}).json()
    assert verdict["accepted"] is True, verdict
    # Queue state is live orchestrator state (not a durable store), so it is
    # enqueued through the running API instance the panel reads.
    queued = environment.client.post(
        "/api/lab/tasks",
        json={"kind": "review", "role": "reviewer",
              "payload": {"detail": "browser acceptance"}}).json()
    assert queued["task"]["kind"] == "review"
    return {"revocable_grant": revocable["grant"]["grant_id"],
            "evidence": evidence}


def test_real_backend_browser_and_cli_parity(lab):
    ids = seed(lab)
    status = lab.client.get("/api/lab/status").json()
    assert status["policy"]["activated"] is True
    assert len(status["queue"]) == 1
    assert status["gpu"]["provider"] == "fake"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(base_url=lab.base_url)
        page.set_default_timeout(10_000)

        # U2: dashboard reads the same live state the API/CLI expose.
        page.goto("/dashboard")
        expect(page.get_by_test_id("policy-activated")).to_have_text("true")
        expect(page.get_by_test_id("dashboard-queue")).to_have_text("1")
        expect(page.get_by_test_id("dashboard-roles")).to_contain_text("reviewer")
        expect(page.get_by_test_id("dashboard-gpu")).to_contain_text('"provider": "fake"')

        # U2: issues and evidence are the backend records, not panel stubs.
        page.get_by_test_id("nav-issues").click()
        expect(page.get_by_test_id("issue-ir.0.0.9.1")).to_contain_text(
            "Browser acceptance candidate")
        expect(page.get_by_test_id("issues-evidence")).to_contain_text("0.0.2.51")

        # U2/U3: dangerous consolidation needs an explicit confirmation and the
        # server decides on rights; a UI confirmation alone is not enough.
        page.get_by_test_id("nav-approvals").click()
        expect(page.get_by_test_id("rights-r-0001")).to_contain_text("consolidate_ir")
        run = page.get_by_test_id("consolidate-run")
        expect(run).to_be_disabled()
        page.get_by_test_id("owner-token").fill(OWNER_TOKEN)
        page.get_by_test_id("consolidate-source").fill("ir.0.0.9.1")
        page.get_by_test_id("consolidate-remaining").fill(
            "U2 browser scenario,U3 reports")
        page.get_by_test_id("consolidate-successors").fill(json.dumps([{
            "identifier": "ir.0.0.9.2",
            "title": "Lab ir successor",
            "transferred_criteria": ["U2 browser scenario", "U3 reports"],
        }], ensure_ascii=False))
        page.get_by_test_id("consolidate-confirm").check()
        run.click()
        expect(page.get_by_test_id("consolidate-outcome")).to_contain_text(
            '"allowed": false')
        expect(page.get_by_test_id("consolidate-outcome")).to_contain_text(
            "no_active_grant")
        assert lab.client.get("/api/lab/transfers").json()["transfers"] == {}

        # The owner grants exactly the missing rights; nothing in the panel
        # changes, yet the identical action now succeeds.
        assert lab.cli("grant", "consolidate_ir", "--subject", "ir.0.0.9.1",
                       "--owner-token", OWNER_TOKEN)["granted"] is True
        assert lab.cli("grant", "delegate_close_ir", "--subject", "ir.0.0.9.1",
                       "--owner-token", OWNER_TOKEN)["granted"] is True
        run.click()
        expect(page.get_by_test_id("consolidate-outcome")).to_contain_text(
            '"closed": true')
        transfers = lab.client.get("/api/lab/transfers").json()["transfers"]
        assert transfers["ir.0.0.9.1"]["closed"] is True
        assert transfers["ir.0.0.9.1"]["successors"] == ["ir.0.0.9.2"]
        expect(page.get_by_test_id("consolidate-transfers")).to_contain_text(
            "ir.0.0.9.2")

        # U2/U3: unauthorized revocation is refused by the server, no matter
        # which client asks - and the grant really stays active.
        grant_id = ids["revocable_grant"]
        assert lab.cli("check", "create_ir_issue", "--subject",
                       "ir.0.0.9.3")["allowed"] is True
        row = page.get_by_test_id(f"grant-{grant_id}")
        page.get_by_test_id("owner-token").fill("wrong-owner-token")
        row.get_by_test_id("revoke-request").click()
        row.get_by_test_id("revoke-confirm").click()
        expect(page.get_by_test_id("approvals-error")).to_contain_text("401")
        grants = lab.client.get("/api/lab/grants").json()["grants"]
        assert any(grant["grant_id"] == grant_id and grant["revoked"] is False
                   for grant in grants)
        denied_cli = lab.cli("revoke", grant_id, "--owner-token", "wrong")
        assert denied_cli["revoked"] is False
        page.get_by_test_id("owner-token").fill(OWNER_TOKEN)
        row.get_by_test_id("revoke-request").click()
        row.get_by_test_id("revoke-confirm").click()
        _wait_until(lambda: any(
            grant["grant_id"] == grant_id and grant["revoked"] is True
            for grant in lab.client.get("/api/lab/grants").json()["grants"]),
            message="revoked grant persisted")
        assert lab.cli("check", "create_ir_issue", "--subject",
                       "ir.0.0.9.3")["allowed"] is False

        # U3: one importance store for Web and Telegram, written as the owner
        # and read back identically by the panel, the API and the CLI.
        page.get_by_test_id("nav-settings").click()
        page.get_by_test_id("notify-web-important").uncheck()
        page.get_by_test_id("notify-telegram-info").check()
        page.get_by_test_id("owner-token").fill("wrong-owner-token")
        page.get_by_test_id("notify-save").click()
        expect(page.get_by_test_id("notify-error")).to_contain_text("401")
        assert lab.client.get("/api/lab/notifications").json()["web"]["important"] is True
        page.get_by_test_id("owner-token").fill(OWNER_TOKEN)
        page.get_by_test_id("notify-save").click()
        expect(page.get_by_test_id("notify-saved")).to_be_visible()
        expected_settings = lab.client.get("/api/lab/notifications").json()
        assert expected_settings["web"]["important"] is False
        assert expected_settings["telegram"]["info"] is True
        assert {key: value for key, value in lab.cli("notify-settings").items()
                if key != "__rc"} == expected_settings

        # U3: the combined report is configurable and identical to the CLI's.
        page.get_by_test_id("nav-reports").click()
        # Live-process sections (queue/status/gpu) belong to the running API
        # instance; the durable shared state is what both clients must agree on.
        for live_section in ("status", "queue", "gpu", "policy"):
            page.get_by_test_id(f"report-section-{live_section}").uncheck()
        page.get_by_test_id("report-fetch").click()
        expect(page.get_by_test_id("report-output")).to_contain_text('"sections"')
        report = json.loads(
            page.get_by_test_id("report-output").text_content() or "{}")
        cli_report = lab.cli("report", "--sections", ",".join(report["sections"]))
        cli_report.pop("__rc")
        cli_report.pop("generated_at")
        report.pop("generated_at")
        assert cli_report == report
        assert not {"status", "queue", "gpu", "policy"} & set(report["sections"])

        # U3: the decision history shown by the panel is the shared audit
        # trail, including the owner refusals and the settings decision.
        page.get_by_test_id("nav-history").click()
        history_table = page.get_by_test_id("history-records")
        expect(history_table).to_contain_text("notification_settings:update")
        expect(history_table).to_contain_text(f"revoke:{grant_id}")
        audit_actions = {record["action"] for record in
                         lab.client.get("/api/lab/audit/records",
                                        params={"limit": 1000}).json()["records"]}
        assert "notification_settings:update" in audit_actions
        assert f"revoke:{grant_id}" in audit_actions

        # U2: GPU surface is backed by the real lease provider status.
        page.get_by_test_id("nav-gpu").click()
        gpu_status = page.get_by_test_id("gpu-status")
        expect(gpu_status).to_contain_text('"provider": "fake"')
        expect(gpu_status).to_contain_text('"idle_sharing_enabled": false')

        browser.close()


def test_panel_outage_does_not_stop_background_protection(lab):
    """U3: the panel is not a liveness condition for the gate or the CLI."""
    seed(lab)
    lab.stop_web()
    decision = lab.cli("check", "push")
    assert decision["allowed"] is False
    assert decision["rule"] == "forbidden_action"
    report = lab.cli("report", "--sections", "status,policy")
    assert report["report"]["policy"]["activated"] is True
    assert report["report"]["status"]["policy"]["activated"] is True
    assert lab.client.get("/api/lab/status").status_code == 200
    records = lab.client.get("/api/lab/audit/records",
                             params={"limit": 1000}).json()["records"]
    assert any(record["action"] == "push" and record["decision"] == "DENIED"
               for record in records)
