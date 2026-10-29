"""The laboratory transport over a controlled GitHub HTTP endpoint (L6).

Issue i.0.0.2.50 (L6) requires the transport tests to call the real
implementation instead of comparing fixture dicts.  These tests run the actual
:class:`lab.governance.GitHubApiTransport` against a local GitHub-shaped HTTP
endpoint: the request, the answer, the refusal, the close and the retry are
all real, and the wiring used by the API and the CLI is exercised too.
"""

import json
import threading
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lab.governance import (GitHubApiTransport, Governance, GovernanceError,
                            IssueTransport, default_transport, issue_scope)
from lab.policy import PolicyGate

TOKEN = "lab-transport-token"


def test_process_crash_after_remote_create_recovers_without_duplicate(tmp_path, github):
    """The OS process dies after POST succeeds, before the local effect record."""
    gate = PolicyGate(tmp_path / "state", activated=True)
    gate.register_grant(actor="lab", action="create_i_issue", subject="i.0.0.0.1",
                        scope_version=issue_scope("i", "Approved implementation", "i.0.0.0.1"),
                        approved_by="owner", expires_at="2099-01-01T00:00:00+00:00")
    script = r'''
import os,sys
from pathlib import Path
from lab.governance import Governance,GitHubApiTransport
from lab.policy import PolicyGate
class CrashTransport(GitHubApiTransport):
 def create_issue(self,*args,**kwargs):
  result=super().create_issue(*args,**kwargs)
  os._exit(73)
root=Path(sys.argv[1])
transport=(CrashTransport if sys.argv[3]=='crash' else GitHubApiTransport)('owner/repo',token='lab-transport-token',base_url=sys.argv[2])
governance=Governance(PolicyGate(root/'state',activated=True),transport,state_dir=root/'gov',repository='owner/repo')
result=governance.create_i_issue('Approved implementation',identifier='i.0.0.0.1',key='approved-scope')
assert result['allowed']
'''
    first = subprocess.run([sys.executable, "-c", script, str(tmp_path), github.url, "crash"], capture_output=True, timeout=30)
    assert first.returncode == 73, first.stderr.decode(errors="replace")
    assert len(github.issues) == 1
    for _ in range(2):
        retry = subprocess.run([sys.executable, "-c", script, str(tmp_path), github.url, "retry"], capture_output=True, timeout=30)
        assert retry.returncode == 0, retry.stderr.decode(errors="replace")
    assert len(github.issues) == 1
    assert len([r for r in github.requests if r["method"] == "POST"]) == 1


class _GitHub:
    """A controlled GitHub HTTP endpoint: issues, comments and closing."""

    def __init__(self, *, status_code: int = 201):
        self.issues: list[dict] = []
        self.comments: list[dict] = []
        self.requests: list[dict] = []
        self.status_code = status_code
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _read(self):
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length) or b"{}")

            def _answer(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                state.requests.append({"method": "GET", "path": path, "query": query,
                                       "headers": dict(self.headers)})
                if path.endswith("/issues"):
                    self._answer(200, state.issues)
                    return
                if path.endswith("/comments"):
                    self._answer(200, state.comments)
                    return
                if "/issues/" in path:
                    number = int(path.rsplit("/", 1)[1])
                    match = next((issue for issue in state.issues if issue["number"] == number), None)
                    self._answer(200 if match else 404, match or {"message": "not found"})
                    return
                self._answer(404, {"message": "not found"})

            def do_POST(self):
                payload = self._read()
                state.requests.append({"method": "POST", "path": self.path,
                                       "headers": dict(self.headers),
                                       "payload": payload})
                if self.path.endswith("/comments"):
                    state.comments.append(payload)
                    self._answer(201, {"id": len(state.comments)})
                    return
                if self.path.endswith("/issues"):
                    if state.status_code >= 400:
                        self._answer(state.status_code, {"message": "refused"})
                        return
                    number = 100 + len(state.issues)
                    state.issues.append({"number": number,
                                         "title": payload.get("title"),
                                         "body": payload.get("body"),
                                         "labels": payload.get("labels", []),
                                         "state": "open"})
                    self._answer(state.status_code,
                                 {"number": number,
                                  "html_url": f"https://github.test/issues/{number}"})
                    return
                self._answer(404, {"message": "not found"})

            def do_PATCH(self):
                payload = self._read()
                state.requests.append({"method": "PATCH", "path": self.path,
                                       "headers": dict(self.headers),
                                       "payload": payload})
                number = int(self.path.rstrip("/").split("/")[-1])
                for issue in state.issues:
                    if issue["number"] == number:
                        issue["state"] = payload.get("state", "closed")
                self._answer(200, {"number": number,
                                   "state": payload.get("state", "closed")})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def github():
    service = _GitHub()
    yield service
    service.close()


def _transport(github, **kwargs) -> GitHubApiTransport:
    return GitHubApiTransport("owner/repo", token=TOKEN, base_url=github.url, **kwargs)


def _headers(request) -> dict:
    """HTTP header names are case-insensitive; the wire form is normalised."""
    return {str(key).lower(): value for key, value in request["headers"].items()}


# -- the real transport over the wire ---------------------------------------
def test_create_issue_reaches_the_controlled_endpoint(github):
    transport = _transport(github)
    issue = transport.create_issue("Підтверджений дефект", "Доказ",
                                   labels=("lab",))
    assert issue["number"] == 100
    assert issue["dry_run"] is False
    assert issue["url"].endswith("/100")

    request = github.requests[-1]
    headers = _headers(request)
    assert request["method"] == "POST"
    assert request["path"] == "/repos/owner/repo/issues"
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == GitHubApiTransport.API_VERSION
    assert request["payload"]["title"] == "Підтверджений дефект"
    assert request["payload"]["body"] == "Доказ"
    assert request["payload"]["labels"] == ["lab"]
    assert github.issues[0]["title"] == "Підтверджений дефект"


def test_find_duplicates_reports_only_similar_titles(github):
    github.issues.extend([
        {"number": 11, "title": "Підтверджений дефект"},
        {"number": 12, "title": "Зовсім інша тема"},
        {"number": 13, "title": "Підтверджений дефект у регресії",
         "pull_request": {"url": "https://github.test/pull/13"}},
    ])
    transport = _transport(github)
    found = transport.find_duplicates("Підтверджений дефект")
    assert [item["number"] for item in found] == [11]
    assert transport.find_duplicates("Немає такого") == []
    assert github.requests[-1]["method"] == "GET"


def test_close_issue_comments_and_closes(github):
    transport = _transport(github)
    issue = transport.create_issue("Дефект", "Доказ")
    closed = transport.close_issue(issue["number"], comment="Закрито за доказом")
    assert closed["state"] == "closed"
    assert closed["dry_run"] is False
    assert github.comments[-1]["body"].startswith("Закрито за доказом\n\n<!-- vertep-lab-close:")
    assert github.issues[0]["state"] == "closed"
    assert github.requests[-1]["method"] == "PATCH"


def test_refused_request_raises_instead_of_looking_published():
    service = _GitHub(status_code=403)
    try:
        transport = _transport(service)
        with pytest.raises(GovernanceError) as error:
            transport.create_issue("Дефект", "Доказ")
    finally:
        service.close()
    assert "HTTP 403" in str(error.value)
    assert service.issues == []


def test_unreachable_endpoint_raises():
    transport = GitHubApiTransport("owner/repo", token=TOKEN,
                                   base_url="http://127.0.0.1:1", timeout=2)
    with pytest.raises(GovernanceError) as error:
        transport.create_issue("Дефект", "Доказ")
    assert "unreachable" in str(error.value)


def test_token_never_leaks_into_the_error():
    service = _GitHub(status_code=403)
    try:
        transport = _transport(service)
        with pytest.raises(GovernanceError) as error:
            transport.create_issue("Дефект", "Доказ")
    finally:
        service.close()
    assert TOKEN not in str(error.value)
    # The token travels only in the Authorization header of the real request.
    assert all(_headers(request).get("authorization") in (None, f"Bearer {TOKEN}")
               for request in service.requests)


def test_closing_without_an_issue_number_is_refused(github):
    transport = _transport(github)
    with pytest.raises(GovernanceError) as error:
        transport.close_issue("ir.0.0.2.52")
    assert "GitHub number" in str(error.value)


# -- the wiring used by the API and the CLI ---------------------------------
def test_default_transport_stays_dry_run_without_owner_configuration(monkeypatch):
    for name in ("LAB_GITHUB_TOKEN", "LAB_GITHUB_REPOSITORY", "LAB_GITHUB_API_URL"):
        monkeypatch.delenv(name, raising=False)
    transport = default_transport()
    assert isinstance(transport, IssueTransport)
    assert transport.dry_run is True
    assert transport.create_issue("Дефект", "Доказ")["dry_run"] is True

    # A token without a repository is not enough to publish anything.
    monkeypatch.setenv("LAB_GITHUB_TOKEN", TOKEN)
    assert default_transport().dry_run is True


def test_default_transport_uses_the_configured_endpoint(monkeypatch, github):
    monkeypatch.setenv("LAB_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("LAB_GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("LAB_GITHUB_API_URL", github.url)
    transport = default_transport()
    assert isinstance(transport, GitHubApiTransport)
    assert transport.dry_run is False
    assert transport.base_url == github.url


def test_governance_publishes_through_the_real_transport(tmp_path, github):
    gate = PolicyGate(tmp_path / "state", activated=True)
    gate.register_grant(actor="lab", action="create_lab_issue",
                        subject="lab.0.0.0.1", approved_by="owner",
                        expires_at="2099-01-01T00:00:00+00:00")
    governance = Governance(gate, _transport(github), state_dir=tmp_path / "gov",
                            repository="owner/repo")
    finding = {
        "identifier": "lab.0.0.0.1",
        "kind": "defect",
        "title": "Підтверджений дефект транспорту",
        "evidence": "Тест падає на кожному запуску",
        "reproduction": "python -m pytest tests/test_lab_github_transport.py",
        "risk": "лабораторія не може опублікувати підтверджену знахідку",
        "expected_result": "знахідка публікується один раз",
        "boundaries": "лише lab, без i/ir/var",
        "source": "#107",
        "approved_scope": "AGENTS.md §4.6",
    }
    outcome = governance.propose_lab_issue(finding, key="lab.0.0.0.1")
    assert outcome["created"] is True
    assert outcome["issue"]["number"] == 100
    assert github.issues[0]["title"] == "lab.0.0.0.1 — Підтверджений дефект транспорту"
    assert github.issues[0]["labels"] == ["lab"]

    # A retry must not publish a second issue.
    retry = governance.propose_lab_issue(finding, key="lab.0.0.0.1")
    assert retry["created"] is False
    assert retry["reason"] == "already created"
    assert len(github.issues) == 1

    records = governance.created()
    assert records[0]["issue"]["number"] == 100
    assert records[0]["key"] == "lab.0.0.0.1"
