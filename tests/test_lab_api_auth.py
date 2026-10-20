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
