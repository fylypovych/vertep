"""The laboratory CLI is a thin client of the shared policy gate (L4/L5/L7).

Every test invokes ``lab.cli.main`` — the real command surface — and asserts
the outcome of the same ``PolicyGate`` the API and the agents use, plus the
durable records and the audit trail.  No permission decision is mocked and no
run reports COMPLETED without a real role invocation over the wire (L1/L2/L6).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lab.cli import main
from lab.policy import PolicyGate
from lab.workspace import FORBIDDEN_ENVIRONMENT

OWNER_TOKEN = "owner-secret"


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    monkeypatch.delenv("LAB_ROLE_EXECUTOR_URL", raising=False)
    # The isolation check inspects the real process environment; strip any
    # production variable inherited from the host so only the copy is seen.
    for name in FORBIDDEN_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    # The owner has switched the gate on; permissions still come from grants.
    monkeypatch.setenv("LAB_POLICY_GATE_ACTIVE", "true")
    # The owner secret is configured on the laboratory host; a grant may only
    # be minted by presenting it (Issue i.0.0.2.50, L4).
    monkeypatch.setenv("LAB_OWNER_TOKEN", OWNER_TOKEN)
    monkeypatch.setenv("LAB_OWNER_IDENTITY", "owner")
    monkeypatch.delenv("LAB_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("LAB_GITHUB_REPOSITORY", raising=False)
    state = tmp_path / "state"
    workspace = tmp_path / "repo"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "README.md").write_text("# lab", encoding="utf-8")
    (workspace / "tests" / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8")
    base = ["--state-dir", str(state), "--workspace", str(workspace)]
    return {"state": state, "workspace": workspace, "base": base}


def _created(state: Path) -> list:
    path = Path(state) / "governance" / "created-issues.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def test_create_issue_is_refused_without_a_grant(paths, capsys):
    rc = main([*paths["base"], "create-issue", "i", "Issue без погодження"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["allowed"] is False
    assert out["rule"] == "no_active_grant"
    assert _created(paths["state"]) == []


# -- L4: a grant is an owner act, never a self-asserted name ----------------
def test_grant_without_the_owner_secret_is_refused(paths, capsys):
    rc = main([*paths["base"], "grant", "create_i_issue",
               "--subject", "i.0.0.1.9", "--approved-by", "owner"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["granted"] is False
    assert "owner authentication" in out["reason"]
    assert PolicyGate(paths["state"]).grants() == []

    # The action stays closed: naming an owner buys nothing.
    rc = main([*paths["base"], "create-issue", "i", "Issue без погодження",
               "--identifier", "i.0.0.1.9"])
    denied = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert denied["rule"] == "no_active_grant"
    assert _created(paths["state"]) == []


def test_grant_with_a_wrong_owner_secret_is_refused(paths, capsys):
    rc = main([*paths["base"], "grant", "create_i_issue",
               "--subject", "i.0.0.1.9", "--owner-token", "not-the-secret"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["granted"] is False
    assert PolicyGate(paths["state"]).grants() == []


def test_grant_identity_comes_from_the_authenticated_owner(paths, capsys, monkeypatch):
    monkeypatch.setenv("LAB_OWNER_IDENTITY", "owner-ua")
    rc = main([*paths["base"], "grant", "create_i_issue",
               "--subject", "i.0.0.1.9", "--owner-token", OWNER_TOKEN])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["grant"]["approved_by"] == "owner-ua"

    # An identity that contradicts the authenticated owner is refused loudly.
    rc = main([*paths["base"], "grant", "create_ir_issue",
               "--subject", "ir.0.0.1.9", "--owner-token", OWNER_TOKEN,
               "--approved-by", "someone-else"])
    refused = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert refused["granted"] is False
    assert "authenticated" in refused["reason"]


def test_revoke_requires_the_owner_secret(paths, capsys):
    rc = main([*paths["base"], "grant", "create_i_issue",
               "--subject", "i.0.0.1.9", "--owner-token", OWNER_TOKEN])
    assert rc == 0
    grant = json.loads(capsys.readouterr().out)["grant"]

    rc = main([*paths["base"], "revoke", grant["grant_id"]])
    refused = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert refused["revoked"] is False
    assert PolicyGate(paths["state"]).active_grant(
        "lab", "create_i_issue", "i.0.0.1.9") is not None

    rc = main([*paths["base"], "revoke", grant["grant_id"],
               "--owner-token", OWNER_TOKEN])
    revoked = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert revoked["revoked"] is True
    assert PolicyGate(paths["state"]).active_grant(
        "lab", "create_i_issue", "i.0.0.1.9") is None


def test_one_owner_grant_creates_exactly_one_issue_and_no_more(paths, capsys):
    rc = main([*paths["base"], "grant", "create_i_issue",
               "--subject", "i.0.0.1.9", "--owner-token", OWNER_TOKEN])
    assert rc == 0
    capsys.readouterr()

    rc = main([*paths["base"], "create-issue", "i", "Затверджений implementation",
               "--identifier", "i.0.0.1.9"])
    first = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert first["created"] is True
    assert first["identifier"] == "i.0.0.1.9"

    # The same approval never yields a second issue: a retry is idempotent.
    rc = main([*paths["base"], "create-issue", "i", "Затверджений implementation",
               "--identifier", "i.0.0.1.9"])
    retry = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert retry["created"] is False
    assert retry["reason"] == "already created"

    # The grant is bound to one subject; another identifier is refused.
    rc = main([*paths["base"], "create-issue", "i", "Не погоджений issue",
               "--identifier", "i.0.0.1.10"])
    other = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert other["allowed"] is False
    assert other["rule"] == "no_active_grant"

    assert [record["identifier"] for record in _created(paths["state"])] == ["i.0.0.1.9"]


def test_rights_request_is_recorded_but_grants_nothing(paths, capsys):
    rc = main([*paths["base"], "rights-request", "create_lab_issue",
               "--subject", "lab.0.0.0.1", "--reason", "підтверджений дефект"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["requested"] is True
    assert out["request"]["state"] == "PENDING"

    gate = PolicyGate(paths["state"])
    recorded = gate.rights_requests()
    assert len(recorded) == 1
    assert recorded[0]["action"] == "create_lab_issue"

    decision = gate.decide("create_lab_issue", actor="lab", subject="lab.0.0.0.1")
    assert decision.allowed is False
    assert decision.rule == "no_active_grant"

    rc = main([*paths["base"], "check", "create_lab_issue",
               "--subject", "lab.0.0.0.1"])
    check = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert check["allowed"] is False


def test_rights_request_refuses_forbidden_actions(paths, capsys):
    rc = main([*paths["base"], "rights-request", "push", "--reason", "автопуш"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["requested"] is False
    assert "can never be requested" in out["reason"]


def test_gpu_lease_denies_a_role_outside_its_profile(paths, capsys):
    rc = main([*paths["base"], "--actor", "reviewer", "--gpu", "gpu", "request",
               "--model", "qwen2.5-coder:7b"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["allowed"] is False
    assert out["decision"]["rule"] == "role_permission"


def test_gpu_lease_request_and_release_through_the_cli(paths, capsys):
    rc = main([*paths["base"], "--gpu", "gpu", "request",
               "--model", "qwen2.5-coder:7b"])
    granted = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert granted["granted"] is True

    rc = main([*paths["base"], "--gpu", "gpu", "release"])
    released = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert isinstance(released, dict)


def test_run_without_an_executor_is_blocked_not_completed(paths, capsys):
    rc = main([*paths["base"], "run"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["state"] == "BLOCKED"
    assert "no role executor configured" in out["reason"]

    rc = main([*paths["base"], "status"])
    status = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert status["executor"] == {"configured": False}


@pytest.fixture()
def invoker():
    """A real local role-execution endpoint honouring the executor contract."""
    received: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            received.append({"path": self.path,
                             "contract": self.headers.get("X-Vertep-Contract"),
                             "body": body})
            payload = json.dumps(
                {"status": "ok",
                 "result": {"invoked": True, "role": body.get("role")}}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # pragma: no cover - keep pytest quiet
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", received
    finally:
        server.shutdown()
        server.server_close()


def test_run_reaches_a_real_role_invocation_over_http(paths, capsys, invoker):
    url, received = invoker
    rc = main([*paths["base"], "--role-executor-url", url, "run"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["state"] == "COMPLETED"
    # Exactly one wire invocation, with the verified contract, never a fake.
    assert len(received) == 1
    assert received[0]["path"] == "/invoke"
    assert received[0]["contract"] == "role-executor/1"
    assert received[0]["body"]["contract_version"] == "role-executor/1"
    assert received[0]["body"]["role"] == "reviewer"

    rc = main([*paths["base"], "audit"])
    audit = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert audit["valid"] is True


# -- U2/U3: the CLI reads the very same settings and reports as the panel ----
def test_notify_settings_show_the_shared_store(paths, capsys):
    from lab.notifications import DEFAULTS
    rc = main([*paths["base"], "notify-settings"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == DEFAULTS


def test_report_sections_match_the_shared_builder(paths, capsys):
    rc = main([*paths["base"], "report", "--sections", "status,budget"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["sections"] == ["status", "budget"]
    assert set(out["report"]) == {"status", "budget"}

    rc = main([*paths["base"], "report", "--sections", "secrets"])
    bad = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "unknown section" in bad["error"]


def test_cli_and_web_read_the_same_settings_and_reports(paths, capsys, monkeypatch):
    """One backend state: what the CLI prints is what the panel will GET."""
    from fastapi.testclient import TestClient
    from lab.api import create_app

    monkeypatch.setenv("LAB_WORKSPACE", str(paths["workspace"]))
    client = TestClient(create_app(state_dir=paths["state"],
                                   workspace_root=paths["workspace"]))

    rc = main([*paths["base"], "notify-settings"])
    cli_settings = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert client.get("/api/lab/notifications").json() == cli_settings

    rc = main([*paths["base"], "report", "--sections", "policy,roles"])
    cli_report = json.loads(capsys.readouterr().out)
    assert rc == 0
    web_report = client.get("/api/lab/reports",
                            params={"sections": "policy,roles"}).json()
    assert web_report["sections"] == cli_report["sections"]
    assert web_report["report"] == cli_report["report"]
