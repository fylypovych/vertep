"""#442 D8: after logout the old session cookie no longer works (stand 0.0.4.41)."""
import base64
import json
import stat

from fastapi.testclient import TestClient


def test_logout_revokes_the_session_on_the_server(monkeypatch, tmp_path):
    from core.app import app
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "logout-contract-password")
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("USERS_JSON", "{}")
    (tmp_path / "installation.json").write_text(json.dumps({"installation_id": "t", "completed_at": "2026-01-01T00:00:00+00:00"}))
    client = TestClient(app)
    login = client.post("/api/session", headers={"Authorization": "Basic " + base64.b64encode(
        b"admin:logout-contract-password").decode()})
    assert login.status_code == 200
    cookies = {"vertep_session": login.cookies["vertep_session"], "vertep_csrf": login.cookies["vertep_csrf"]}
    other = TestClient(app).post("/api/session", headers={"Authorization": "Basic " + base64.b64encode(
        b"admin:logout-contract-password").decode()}).cookies["vertep_session"]
    assert TestClient(app).get("/api/jobs", cookies=cookies).status_code == 200
    out = TestClient(app).delete("/api/session", cookies=cookies, headers={"X-CSRF-Token": cookies["vertep_csrf"]})
    assert out.status_code == 200
    replay = TestClient(app)
    session = replay.get("/api/session", cookies=cookies)
    assert session.status_code == 401 or session.json()["authenticated"] is False
    assert replay.get("/api/jobs", cookies=cookies).status_code == 401
    assert TestClient(app).get("/api/jobs", cookies={"vertep_session": other}).status_code == 200  # other session lives
    assert stat.S_IMODE((tmp_path / "revoked-sessions.json").stat().st_mode) == 0o600


def test_a_forged_cookie_is_not_recorded(monkeypatch, tmp_path):
    from core.security import revoke_session
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    revoke_session("99999999999:admin:admin:x.forged-signature")
    assert not (tmp_path / "revoked-sessions.json").exists()
