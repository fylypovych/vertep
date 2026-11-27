"""#491 L3 / #493 AC6: the LAB accepts a CORE-signed administrator assertion."""

import pytest


def test_core_assertion_is_verified_by_the_lab(tmp_path, monkeypatch):
    from core import lab_identity
    from lab import identity
    monkeypatch.setenv("LAB_IDENTITY_KEY_FILE", str(tmp_path / "lab-identity.key"))
    monkeypatch.setenv("LAB_VERTEP_IDENTITY_KEY", lab_identity.public_key_pem())
    identity.reset_cache()
    token = lab_identity.issue("root", "admin", now=1000)
    assert identity.verify(token, now=1100) == "root"
    with pytest.raises(identity.IdentityError):
        identity.verify(token, now=1000 + lab_identity.LIFETIME_SECONDS)  # expired
    body, signature = token.split(".")
    with pytest.raises(identity.IdentityError):
        identity.verify(body + "x." + signature, now=1100)  # tampered
    with pytest.raises(PermissionError):
        lab_identity.issue("mod", "moderator")
    monkeypatch.setenv("LAB_PANEL_URL", "https://lab.example:8443")
    assert lab_identity.allowed_return("https://lab.example:8443/")
    assert not lab_identity.allowed_return("https://evil.example/")
    identity.reset_cache()


# -- #491 L3 over HTTP: CORE issues, the LAB API accepts ------------------------------

def _core_session(monkeypatch, tmp_path, user, role):
    import hashlib
    import hmac
    import json as _json
    from core.security import _session_token
    monkeypatch.setenv("ADMIN_PASSWORD", "lab-identity-password")
    monkeypatch.delenv("CONFIG_ROOT", raising=False)
    monkeypatch.setenv("USERS_JSON", _json.dumps({user: {"password_hash": "x", "role": role}}))
    monkeypatch.setenv("LAB_IDENTITY_KEY_FILE", str(tmp_path / "lab-identity.key"))
    monkeypatch.setenv("VERTEP_MODULE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LAB_PANEL_URL", "https://lab.example:8444")
    token = _session_token(user, role)
    return {"vertep_session": token,
            "vertep_csrf": hmac.new(b"lab-identity-password", token.encode(), hashlib.sha256).hexdigest()}


def _core_client():
    from fastapi.testclient import TestClient
    from core.app import app
    return TestClient(app, follow_redirects=False)


def _audit(tmp_path):
    import json as _json
    return [_json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()
            if '"lab.identity"' in line]


def test_core_lab_login_admin_redirects_with_a_verifiable_fragment(tmp_path, monkeypatch):
    from core import lab_identity
    from lab import identity
    cookies = _core_session(monkeypatch, tmp_path, "admin", "admin")
    response = _core_client().get("/api/lab/login", params={"return_to": "https://lab.example:8444/"},
                                  cookies=cookies)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://lab.example:8444/#vertep_identity=")
    monkeypatch.setenv("LAB_VERTEP_IDENTITY_KEY", lab_identity.public_key_pem())
    identity.reset_cache()
    assert identity.verify(location.split("#vertep_identity=", 1)[1]) == "admin"
    identity.reset_cache()
    assert _audit(tmp_path)[-1]["outcome"] == "allowed" and _audit(tmp_path)[-1]["actor"] == "admin"
    panel = _core_client().get("/api/lab/panel", cookies=cookies)
    assert panel.json() == {"url": "https://lab.example:8444"}


def test_core_lab_login_refuses_moderator_and_foreign_return(tmp_path, monkeypatch):
    cookies = _core_session(monkeypatch, tmp_path, "reader", "moderator")
    client = _core_client()
    response = client.get("/api/lab/login", params={"return_to": "https://lab.example:8444/"}, cookies=cookies)
    assert response.status_code == 403
    assert _audit(tmp_path)[-1] == {**_audit(tmp_path)[-1], "outcome": "denied", "reason": "not_admin"}
    assert client.get("/api/lab/panel", cookies=cookies).status_code == 403
    cookies = _core_session(monkeypatch, tmp_path, "admin", "admin")
    response = client.get("/api/lab/login", params={"return_to": "https://evil.example/"}, cookies=cookies)
    assert response.status_code == 422
    assert _audit(tmp_path)[-1]["reason"] == "return_to"


def test_lab_api_accepts_the_assertion_and_refuses_a_forged_or_expired_one(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core import lab_identity
    from lab import identity
    from lab.api import create_app
    monkeypatch.setenv("LAB_IDENTITY_KEY_FILE", str(tmp_path / "lab-identity.key"))
    monkeypatch.setenv("LAB_VERTEP_IDENTITY_KEY", lab_identity.public_key_pem())
    monkeypatch.delenv("LAB_OWNER_TOKEN", raising=False)
    identity.reset_cache()
    client = TestClient(create_app(state_dir=tmp_path / "state", workspace_root=tmp_path / "repo"))
    token = lab_identity.issue("root", "admin")
    granted = client.post("/api/lab/grants", headers={"X-Vertep-Identity": token}, json={
        "actor": "lab", "action": "create_lab_issue", "subject": "lab.0.0.0.1",
        "approved_by": "someone", "expires_at": "2099-01-01T00:00:00+00:00"})
    assert granted.status_code == 200 and granted.json()["grant"]["approved_by"] == "vertep:root"
    body, signature = token.split(".")
    forged = body[:-2] + ("A" if body[-2] != "A" else "B") + body[-1] + "." + signature
    expired = lab_identity.issue("root", "admin", now=1000)
    for bad in (forged, expired):
        assert client.get("/api/lab/status", headers={"X-Vertep-Identity": bad}).status_code == 401
    identity.reset_cache()


def test_administrator_sets_the_lab_panel_address_in_settings(tmp_path, monkeypatch):
    """#491 L2: the optional LAB connection is set in Settings and survives a reload;
    only an HTTPS address is accepted, a moderator cannot change it, and empty clears it."""
    from core import first_run
    monkeypatch.setattr(first_run, "config_root", lambda: tmp_path / "config")
    cookies = _core_session(monkeypatch, tmp_path, "admin", "admin")
    monkeypatch.delenv("LAB_PANEL_URL")
    client = _core_client()
    headers = {"X-CSRF-Token": cookies["vertep_csrf"]}
    assert client.get("/api/settings/lab-panel", cookies=cookies).json() == {"url": None}
    saved = client.put("/api/settings/lab-panel", json={"url": "https://10.0.0.5:8444/"}, cookies=cookies, headers=headers)
    assert saved.status_code == 200, saved.text
    assert client.get("/api/settings/lab-panel", cookies=cookies).json() == {"url": "https://10.0.0.5:8444"}
    assert client.get("/api/lab/panel", cookies=cookies).json() == {"url": "https://10.0.0.5:8444"}
    login = client.get("/api/lab/login", params={"return_to": "https://10.0.0.5:8444/"}, cookies=cookies)
    assert login.status_code == 302
    for bad in ("http://10.0.0.5:8444", "https://user:pw@lab", "https://lab/?x=1", "lab:8444", "https://lab:99999"):
        assert client.put("/api/settings/lab-panel", json={"url": bad}, cookies=cookies, headers=headers).status_code == 422
    assert client.get("/api/settings/lab-panel", cookies=cookies).json() == {"url": "https://10.0.0.5:8444"}
    moderator = _core_session(monkeypatch, tmp_path, "reader", "moderator")
    refused = client.put("/api/settings/lab-panel", json={"url": "https://evil.example"}, cookies=moderator,
                         headers={"X-CSRF-Token": moderator["vertep_csrf"]})
    assert refused.status_code == 403
    assert client.get("/api/settings/lab-panel", cookies=moderator).status_code == 403
    cookies = _core_session(monkeypatch, tmp_path, "admin", "admin")
    monkeypatch.delenv("LAB_PANEL_URL")
    headers = {"X-CSRF-Token": cookies["vertep_csrf"]}
    assert client.put("/api/settings/lab-panel", json={"url": ""}, cookies=cookies, headers=headers).json() == {"url": None}
    assert client.get("/api/lab/panel", cookies=cookies).json() == {"url": None}


def test_lab_frame_gives_the_admin_the_panel_with_a_verifiable_assertion(tmp_path, monkeypatch):
    """#491 L2: the LAB panel inside Vertep — same assertion as /api/lab/login, for the frame."""
    from core import lab_identity
    from lab import identity
    cookies = _core_session(monkeypatch, tmp_path, "admin", "admin")
    framed = _core_client().get("/api/lab/frame", cookies=cookies).json()
    assert framed["panel"] == "https://lab.example:8444"
    assert framed["url"].startswith("https://lab.example:8444/#vertep_identity=")
    monkeypatch.setenv("LAB_VERTEP_IDENTITY_KEY", lab_identity.public_key_pem())
    identity.reset_cache()
    assert identity.verify(framed["url"].split("#vertep_identity=", 1)[1]) == "admin"
    identity.reset_cache()
    moderator = _core_session(monkeypatch, tmp_path, "reader", "moderator")
    assert _core_client().get("/api/lab/frame", cookies=moderator).status_code == 403


def test_lab_panel_may_be_framed_only_by_the_connected_vertep():
    from pathlib import Path
    root = Path(__file__).parents[1]
    nginx = (root / "deploy/lab/web/nginx.conf").read_text(encoding="utf-8")
    assert nginx.count("include /tmp/lab-frame.conf;") == 2  # server and the index.html location
    dockerfile = (root / "deploy/lab/Dockerfile.web").read_text(encoding="utf-8")
    assert "frame-ancestors" in dockerfile and "LAB_VERTEP_URL" in dockerfile
    compose = (root / "deploy/lab/docker-compose.web.yml").read_text(encoding="utf-8")
    assert "LAB_VERTEP_URL: ${LAB_VERTEP_URL:-}" in compose.split("lab-web:", 1)[1]
