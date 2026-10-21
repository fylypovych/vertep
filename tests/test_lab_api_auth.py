"""API authorisation for the laboratory grant endpoint (Issue i.0.0.2.50, L4)."""

import pytest
from fastapi.testclient import TestClient

from lab.api import create_app


def build_client(tmp_path):
    """Build the laboratory API on a temporary state directory and working copy.

    The production defaults (``/var/lib/vertep-lab`` and ``/srv/vertep-lab/repo``)
    belong to the laboratory host, so a test never uses them.
    """
    return TestClient(create_app(state_dir=tmp_path / "state",
                                 workspace_root=tmp_path / "repo"))


def test_grant_requires_owner_token(monkeypatch, tmp_path):
    monkeypatch.delenv("LAB_OWNER_TOKEN", raising=False)
    client = build_client(tmp_path)
    response = client.post("/api/lab/grants", json={
        "actor": "lab", "action": "create_lab_issue", "subject": "lab.0.0.0.1",
        "approved_by": "lab", "expires_at": "2099-01-01T00:00:00+00:00",
    })
    assert response.status_code == 401


def test_grant_rejects_wrong_owner_token(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_OWNER_TOKEN", "secret")
    client = build_client(tmp_path)
    response = client.post("/api/lab/grants", json={
        "actor": "lab", "action": "create_lab_issue", "subject": "lab.0.0.0.1",
        "approved_by": "lab", "expires_at": "2099-01-01T00:00:00+00:00",
    }, headers={"X-Lab-Owner-Token": "wrong"})
    assert response.status_code == 401


def test_grant_rejects_self_asserted_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_OWNER_TOKEN", "secret")
    client = build_client(tmp_path)
    # Even with a valid token, approved_by from the body must not be trusted;
    # the endpoint overrides it with the authenticated owner identity.
    response = client.post("/api/lab/grants", json={
        "actor": "lab", "action": "create_lab_issue", "subject": "lab.0.0.0.1",
        "approved_by": "lab", "expires_at": "2099-01-01T00:00:00+00:00",
    }, headers={"X-Lab-Owner-Token": "secret"})
    assert response.status_code == 200
    assert response.json()["grant"]["approved_by"] == "owner"


def test_policy_rejects_self_approval(tmp_path):
    from datetime import datetime, timedelta, timezone
    from lab.policy import PolicyGate
    gate = PolicyGate(tmp_path / "state", activated=True)
    with pytest.raises(ValueError):
        gate.register_grant(actor="lab", action="create_lab_issue", subject="lab.0.0.0.1",
                            approved_by="lab", expires_at=(datetime.now(timezone.utc) +
                                                          timedelta(hours=8)).isoformat())


# -- L4: revocation is an owner act too -----------------------------------
def test_revoke_requires_owner_token(monkeypatch, tmp_path):
    monkeypatch.delenv("LAB_OWNER_TOKEN", raising=False)
    client = build_client(tmp_path)
    assert client.post("/api/lab/grants/any/revoke").status_code == 401

    monkeypatch.setenv("LAB_OWNER_TOKEN", "secret")
    assert client.post("/api/lab/grants/any/revoke",
                       headers={"X-Lab-Owner-Token": "wrong"}).status_code == 401


def test_revoke_uses_the_authenticated_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_OWNER_TOKEN", "secret")
    monkeypatch.delenv("LAB_OWNER_IDENTITY", raising=False)
    client = build_client(tmp_path)
    grant = client.post("/api/lab/grants", json={
        "actor": "lab", "action": "create_lab_issue", "subject": "lab.0.0.0.1",
        "approved_by": "lab", "expires_at": "2099-01-01T00:00:00+00:00",
    }, headers={"X-Lab-Owner-Token": "secret"}).json()["grant"]

    response = client.post(f"/api/lab/grants/{grant['grant_id']}/revoke",
                           headers={"X-Lab-Owner-Token": "secret"})
    assert response.status_code == 200
    assert response.json()["revoked"] is True

    gate = client.app.state.context["gate"]
    revocations = [r for r in gate.trail.records()
                   if str(r.get("action", "")).startswith("revoke:")]
    assert revocations and revocations[-1]["actor"] == "owner"


def test_owner_token_never_appears_in_the_audit_trail(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_OWNER_TOKEN", "top-secret-token")
    client = build_client(tmp_path)
    client.post("/api/lab/grants", json={
        "actor": "lab", "action": "create_lab_issue", "subject": "lab.0.0.0.1",
        "approved_by": "lab", "expires_at": "2099-01-01T00:00:00+00:00",
    }, headers={"X-Lab-Owner-Token": "top-secret-token"})
    gate = client.app.state.context["gate"]
    import json as _json
    assert "top-secret-token" not in _json.dumps(gate.trail.records(), default=str)


# -- L4: the GPU effect is authorised before it happens --------------------
def build_gpu_client(tmp_path, provider):
    return TestClient(create_app(state_dir=tmp_path / "state",
                                 workspace_root=tmp_path / "repo",
                                 provider=provider))


def test_gpu_lease_is_denied_for_a_role_without_the_permission(monkeypatch, tmp_path):
    from lab.gpu_lease import FakeResourceProvider
    provider = FakeResourceProvider()
    client = build_gpu_client(tmp_path, provider)
    response = client.post("/api/lab/gpu/request",
                           json={"actor": "reviewer", "model": "lab-llm",
                                 "vram_mb": 1024})
    assert response.status_code == 403
    assert provider.requests == 0
    gate = client.app.state.context["gate"]
    denied = [r for r in gate.trail.records()
              if r.get("action") == "request_gpu_lease"]
    assert denied and denied[-1]["decision"] == "DENIED"
    assert denied[-1]["rule"] == "role_permission"
    assert denied[-1]["actor"] == "reviewer"


def test_gpu_lease_is_granted_within_the_lab_environment(monkeypatch, tmp_path):
    from lab.gpu_lease import FakeResourceProvider
    provider = FakeResourceProvider()
    client = build_gpu_client(tmp_path, provider)
    response = client.post("/api/lab/gpu/request",
                           json={"actor": "ai_gpu", "model": "lab-llm",
                                 "vram_mb": 1024})
    assert response.status_code == 200
    assert response.json()["lease"]["lease_id"].startswith("lease-")
    assert provider.requests == 1


def test_gpu_lease_is_denied_under_production_pressure(monkeypatch, tmp_path):
    from lab.gpu_lease import FakeResourceProvider
    provider = FakeResourceProvider(queue_depth=3)
    client = build_gpu_client(tmp_path, provider)
    response = client.post("/api/lab/gpu/request",
                           json={"actor": "lab", "model": "lab-llm"})
    assert response.status_code == 409
    assert "production queue" in response.json()["detail"]
    assert provider.leases == {}


# -- L5: issue endpoints go through the one gate --------------------------
def _activate(client):
    client.app.state.context["gate"].activated = True
    return client


def test_issue_probe_never_creates_anything(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_OWNER_TOKEN", "secret")
    client = _activate(build_client(tmp_path))
    client.post("/api/lab/grants", json={
        "actor": "lab", "action": "create_ir_issue", "subject": "ir.0.0.0.9",
        "approved_by": "owner", "expires_at": "2099-01-01T00:00:00+00:00",
    }, headers={"X-Lab-Owner-Token": "secret"})

    probe = client.get("/api/lab/issues/ir", params={"subject": "ir.0.0.0.9"})
    assert probe.status_code == 200
    assert probe.json()["allowed"] is True
    governance = client.app.state.context["governance"]
    assert governance.created() == []


def test_issue_creation_is_denied_without_a_grant(monkeypatch, tmp_path):
    monkeypatch.delenv("LAB_OWNER_TOKEN", raising=False)
    client = _activate(build_client(tmp_path))
    response = client.post("/api/lab/issues/ir",
                           json={"title": "Нема погодження"})
    assert response.status_code == 200
    assert response.json()["allowed"] is False
    assert response.json()["rule"] == "no_active_grant"
    governance = client.app.state.context["governance"]
    assert governance.created() == []


def test_one_api_approval_creates_exactly_one_issue(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_OWNER_TOKEN", "secret")
    client = _activate(build_client(tmp_path))
    client.post("/api/lab/grants", json={
        "actor": "lab", "action": "create_ir_issue", "subject": "ir.0.0.0.1",
        "approved_by": "owner", "expires_at": "2099-01-01T00:00:00+00:00",
    }, headers={"X-Lab-Owner-Token": "secret"})

    body = {"title": "Погоджений ir", "identifier": "ir.0.0.0.1"}
    first = client.post("/api/lab/issues/ir", json=body)
    second = client.post("/api/lab/issues/ir", json=body)
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    governance = client.app.state.context["governance"]
    assert len(governance.created()) == 1


def test_push_and_close_probes_are_refused_read_only(tmp_path):
    client = build_client(tmp_path)
    for kind in ("push", "close", "edit"):
        probe = client.get(f"/api/lab/issues/{kind}")
        assert probe.status_code == 200
        assert probe.json()["allowed"] is False
    governance = client.app.state.context["governance"]
    assert governance.created() == []
    assert governance.closed() == {}


def test_unknown_issue_surface_is_a_404(tmp_path):
    client = build_client(tmp_path)
    assert client.get("/api/lab/issues/nope").status_code == 404
    assert client.post("/api/lab/issues/nope",
                       json={"title": "x"}).status_code == 404


# -- panel endpoints: the Web reads the very same state as the CLI (U1/U2) ---
def _finding():
    return {"identifier": "lab.0.0.0.1", "kind": "defect", "title": "Дефект",
            "evidence": "desc", "reproduction": "desc", "risk": "desc",
            "expected_result": "desc", "boundaries": "desc",
            "source": "src", "approved_scope": "scope"}


def _complete_evidence():
    return {"version": "0.0.2.70", "commit": "abc1234",
            "environment": "lab-vm ubuntu-24.04",
            "expected": "усі тести зелені", "actual": "усі тести зелені",
            "reviewer": "qa-agent",
            "test_results": {"passed": 12, "failed": 0, "skipped": 0}}


def test_panel_reads_expose_the_cli_state(monkeypatch, tmp_path):
    monkeypatch.delenv("LAB_OWNER_TOKEN", raising=False)
    client = _activate(build_client(tmp_path))
    gate = client.app.state.context["gate"]
    gate.register_grant(actor="lab", action="create_lab_issue",
                        subject="lab.0.0.0.1", approved_by="owner",
                        expires_at="2099-01-01T00:00:00+00:00")
    assert client.post("/api/lab/issues/lab",
                       json={"finding": _finding()}).json()["created"] is True

    issues = client.get("/api/lab/issues").json()
    assert [item["finding"]["identifier"] for item in issues["created"]] == ["lab.0.0.0.1"]
    assert issues["closed"] == {}

    assert client.get("/api/lab/evidence").json() == {"evidence": []}
    grants = client.get("/api/lab/grants").json()
    assert [grant["action"] for grant in grants["grants"]] == ["create_lab_issue"]
    assert client.get("/api/lab/rights-requests").json() == {"requests": []}
    assert client.get("/api/lab/transfers").json() == {"transfers": {}}
    notifications = client.get("/api/lab/notifications").json()
    assert notifications["web"]["critical"] is True
    assert notifications["telegram"]["info"] is False

    report = client.get("/api/lab/reports",
                        params={"sections": "issues,status"}).json()
    assert report["sections"] == ["issues", "status"]
    assert report["report"]["issues"]["created"][0]["finding"]["identifier"] == "lab.0.0.0.1"
    assert client.get("/api/lab/reports",
                      params={"sections": "secrets"}).status_code == 422


def test_notifications_write_is_an_owner_act(monkeypatch, tmp_path):
    monkeypatch.delenv("LAB_OWNER_TOKEN", raising=False)
    client = build_client(tmp_path)
    payload = {"settings": {"telegram": {"info": True}}}
    assert client.put("/api/lab/notifications", json=payload).status_code == 401

    monkeypatch.setenv("LAB_OWNER_TOKEN", "secret")
    assert client.put("/api/lab/notifications", json=payload,
                      headers={"X-Lab-Owner-Token": "wrong"}).status_code == 401
    assert client.get("/api/lab/notifications").json()["telegram"]["info"] is False

    ok = client.put("/api/lab/notifications", json=payload,
                    headers={"X-Lab-Owner-Token": "secret"})
    assert ok.status_code == 200
    assert client.get("/api/lab/notifications").json()["telegram"]["info"] is True

    bad = client.put("/api/lab/notifications",
                     json={"settings": {"email": {"info": True}}},
                     headers={"X-Lab-Owner-Token": "secret"})
    assert bad.status_code == 422

    gate = client.app.state.context["gate"]
    updates = [r for r in gate.trail.records()
               if r.get("action") == "notification_settings:update"]
    assert updates and updates[-1]["actor"] == "owner"


def test_ir_close_endpoint_requires_owner_and_complete_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_OWNER_TOKEN", "secret")
    client = _activate(build_client(tmp_path))
    client.post("/api/lab/grants", json={
        "actor": "lab", "action": "create_ir_issue", "subject": "ir.0.0.0.1",
        "approved_by": "owner", "expires_at": "2099-01-01T00:00:00+00:00",
    }, headers={"X-Lab-Owner-Token": "secret"})
    client.post("/api/lab/grants", json={
        "actor": "lab", "action": "delegate_close_ir", "subject": "ir.0.0.0.1",
        "approved_by": "owner", "expires_at": "2099-01-01T00:00:00+00:00",
    }, headers={"X-Lab-Owner-Token": "secret"})
    client.post("/api/lab/issues/ir",
                json={"title": "ir до закриття", "identifier": "ir.0.0.0.1"})

    body = {"identifier": "ir.0.0.0.1", "evidence": _complete_evidence()}
    verdict = client.post("/api/lab/ir/evidence", json=body)
    assert verdict.json()["accepted"] is True
    evidence = client.get("/api/lab/evidence").json()
    assert evidence["evidence"][-1]["evidence"]["reviewer"] == "qa-agent"

    assert client.post("/api/lab/ir/close", json=body).status_code == 401
    assert client.post("/api/lab/ir/close", json=body,
                       headers={"X-Lab-Owner-Token": "wrong"}).status_code == 401

    closed = client.post("/api/lab/ir/close", json=body,
                         headers={"X-Lab-Owner-Token": "secret"})
    assert closed.json()["closed"] is True
    issues = client.get("/api/lab/issues").json()
    assert "ir.0.0.0.1" in issues["closed"]


def test_ir_close_refused_for_incomplete_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_OWNER_TOKEN", "secret")
    client = _activate(build_client(tmp_path))
    body = {"identifier": "ir.0.0.0.2",
            "evidence": {**_complete_evidence(),
                         "test_results": {"passed": 3, "failed": 1, "skipped": 0}}}
    verdict = client.post("/api/lab/ir/evidence", json=body).json()
    assert verdict["accepted"] is False

    closed = client.post("/api/lab/ir/close", json=body,
                         headers={"X-Lab-Owner-Token": "secret"}).json()
    assert closed["allowed"] is False
    assert client.app.state.context["governance"].closed() == {}


def test_consolidation_endpoint_requires_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_OWNER_TOKEN", "secret")
    client = _activate(build_client(tmp_path))
    source = {"identifier": "ir.0.0.0.3", "title": "ir з залишком",
              "number": None,
              "remaining_criteria": ["U2 browser scenario", "U3 reports"]}
    for subject in ("ir.0.0.0.3", "ir.0.0.0.4", "ir.0.0.0.5"):
        client.post("/api/lab/grants", json={
            "actor": "lab", "action": "create_ir_issue", "subject": subject,
            "approved_by": "owner", "expires_at": "2099-01-01T00:00:00+00:00",
        }, headers={"X-Lab-Owner-Token": "secret"})
    for action in ("consolidate_ir", "delegate_close_ir"):
        client.post("/api/lab/grants", json={
            "actor": "lab", "action": action, "subject": "ir.0.0.0.3",
            "approved_by": "owner", "expires_at": "2099-01-01T00:00:00+00:00",
        }, headers={"X-Lab-Owner-Token": "secret"})
    client.post("/api/lab/issues/ir",
                json={"title": "ir з залишком", "identifier": "ir.0.0.0.3"})

    payload = {"source": source,
               "successors": [
                   {"title": "Залишок U2", "identifier": "ir.0.0.0.4",
                    "transferred_criteria": ["U2 browser scenario"]},
                   {"title": "Залишок U3", "identifier": "ir.0.0.0.5",
                    "transferred_criteria": ["U3 reports"]},
               ]}
    assert client.post("/api/lab/ir/consolidate",
                       json=payload).status_code == 401
    assert client.post("/api/lab/ir/consolidate", json=payload,
                       headers={"X-Lab-Owner-Token": "wrong"}).status_code == 401

    outcome = client.post("/api/lab/ir/consolidate", json=payload,
                          headers={"X-Lab-Owner-Token": "secret"}).json()
    assert outcome["allowed"] is True
    assert outcome["closed"] is True
    assert outcome["created"] == ["ir.0.0.0.4", "ir.0.0.0.5"]

    transfers = client.get("/api/lab/transfers").json()
    assert transfers["transfers"]["ir.0.0.0.3"]["successors"] == ["ir.0.0.0.4",
                                                                  "ir.0.0.0.5"]
