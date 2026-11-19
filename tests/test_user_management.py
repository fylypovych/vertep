"""Owner decision 19.11.2026: the administrator creates moderators, assigns their
brands and resets passwords in Settings; moderators manage nothing here."""

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from core.app import app
    from core.security import _session_token, session_secret
    config = tmp_path / "config"
    config.mkdir()
    brands = tmp_path / "brands"
    for brand in ("brand-a", "brand-b"):
        (brands / brand).mkdir(parents=True)
        (brands / brand / "brand.json").write_text(json.dumps({"id": brand, "name": brand}))
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("BRANDS_ROOT", str(brands))
    monkeypatch.setenv("ADMIN_USER", "root")
    monkeypatch.setenv("ADMIN_PASSWORD", "root-password-123")
    monkeypatch.setenv("USERS_JSON", "{}")
    (config / "installation.json").write_text(json.dumps({"completed_at": "2026-01-01T00:00:00+00:00"}))

    def client(user, role):
        session = _session_token(user, role)
        secret = session_secret()
        csrf = hmac.new(secret.encode() if isinstance(secret, str) else secret, session.encode(), hashlib.sha256).hexdigest()
        result = TestClient(app, headers={"Authorization": "", "x-csrf-token": csrf})
        result.cookies.set("vertep_session", session)
        result.cookies.set("vertep_csrf", csrf)
        return result

    return {"client": client, "app": app, "config": config}


def _login(app, user, password):
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    return TestClient(app).post("/api/session", headers={"Authorization": auth})


def test_admin_creates_a_moderator_with_brands_who_can_log_in(setup):
    admin = setup["client"]("root", "admin")
    created = admin.post("/api/settings/users", json={"user": "anna", "role": "moderator", "brands": ["brand-a"],
                                                      "password": "anna-password-123"})
    assert created.status_code == 200, created.text
    assert created.json() == {"user": "anna", "role": "moderator", "brands": ["brand-a"], "display_name": None,
                              "email": None, "editable": True}
    stored = json.loads((setup["config"] / "users.json").read_text())["anna"]
    assert stored["password_hash"].startswith("pbkdf2_sha256$") and "anna-password-123" not in json.dumps(stored)
    login = _login(setup["app"], "anna", "anna-password-123")
    assert login.status_code == 200 and login.json()["role"] == "moderator" and login.json()["brands"] == ["brand-a"]
    listed = admin.get("/api/settings/users").json()["users"]
    assert {item["user"] for item in listed} >= {"anna"}
    assert all("password_hash" not in item for item in listed)


def test_admin_changes_brands_role_and_password_and_deletes(setup):
    admin = setup["client"]("root", "admin")
    admin.post("/api/settings/users", json={"user": "anna", "role": "moderator", "brands": ["brand-a"],
                                            "password": "anna-password-123"})
    changed = admin.put("/api/settings/users/anna", json={"brands": ["brand-a", "brand-b"], "password": "new-password-456"})
    assert changed.status_code == 200 and changed.json()["brands"] == ["brand-a", "brand-b"]
    assert _login(setup["app"], "anna", "anna-password-123").status_code == 401
    assert _login(setup["app"], "anna", "new-password-456").status_code == 200
    promoted = admin.put("/api/settings/users/anna", json={"role": "admin"})
    assert promoted.json()["role"] == "admin" and promoted.json()["brands"] is None
    assert admin.delete("/api/settings/users/anna").status_code == 200
    assert _login(setup["app"], "anna", "new-password-456").status_code == 401


@pytest.mark.parametrize("payload, detail", [
    ({"user": "x", "role": "moderator", "password": "long-enough-password"}, "Invalid username"),
    ({"user": "bob", "role": "viewer2", "password": "long-enough-password"}, "role must be admin or moderator"),
    ({"user": "bob", "role": "moderator", "brands": ["ghost"], "password": "long-enough-password"}, "Unknown brands: ghost"),
    ({"user": "bob", "role": "moderator", "brands": ["brand-a"], "password": "short"}, None),
    ({"user": "root", "role": "moderator", "password": "long-enough-password"}, "User already exists"),
])
def test_invalid_accounts_are_refused(setup, payload, detail):
    response = setup["client"]("root", "admin").post("/api/settings/users", json=payload)
    assert response.status_code in {409, 422}, response.text
    if detail:
        assert response.json()["detail"] == detail
    assert not (setup["config"] / "users.json").exists() or "bob" not in (setup["config"] / "users.json").read_text()


def test_moderators_cannot_manage_accounts_and_admin_cannot_lock_itself_out(setup):
    admin = setup["client"]("root", "admin")
    admin.post("/api/settings/users", json={"user": "boss", "role": "admin", "password": "boss-password-123"})
    admin.post("/api/settings/users", json={"user": "anna", "role": "moderator", "brands": ["brand-a"],
                                            "password": "anna-password-123"})
    moderator = setup["client"]("anna", "moderator")
    assert moderator.get("/api/settings/users").status_code == 403
    assert moderator.post("/api/settings/users", json={"user": "eve", "role": "admin",
                                                       "password": "eve-password-1234"}).status_code == 403
    assert moderator.put("/api/settings/users/anna", json={"brands": ["brand-b"]}).status_code == 403
    boss = setup["client"]("boss", "admin")
    assert boss.put("/api/settings/users/boss", json={"role": "moderator"}).status_code == 409
    assert boss.delete("/api/settings/users/boss").status_code == 409
    assert admin.put("/api/settings/users/root", json={"role": "moderator"}).status_code == 409  # installation admin
