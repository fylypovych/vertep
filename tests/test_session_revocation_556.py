"""#556 (i.0.0.5.56): revoke active user sessions on account deletion, role demotion, or password change."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import os
import stat
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    from core.app import app

    config = tmp_path / "config"
    config.mkdir()
    brands = tmp_path / "brands"
    for brand in ("brand-a", "brand-b"):
        (brands / brand).mkdir(parents=True)
        (brands / brand / "brand.json").write_text(json.dumps({"id": brand, "name": brand}), encoding="utf-8")

    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("BRANDS_ROOT", str(brands))
    monkeypatch.setenv("ADMIN_USER", "root")
    monkeypatch.setenv("ADMIN_PASSWORD", "root-password-1234")
    monkeypatch.setenv("USERS_JSON", "{}")
    (config / "installation.json").write_text(
        json.dumps({"installation_id": "test-556", "completed_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    return {"app": app, "config": config, "brands": brands}


def _login(app, user: str, password: str):
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return TestClient(app).post("/api/session", headers={"Authorization": auth})


def _session_cookies(response) -> tuple[dict[str, str], dict[str, str]]:
    cookies = {
        "vertep_session": response.cookies["vertep_session"],
        "vertep_csrf": response.cookies["vertep_csrf"],
    }
    return cookies, {"x-csrf-token": cookies["vertep_csrf"]}


def _api(app, cookies: dict[str, str], headers: dict[str, str] | None = None) -> TestClient:
    client = TestClient(app, headers={"Authorization": "", **(headers or {})})
    for key, value in cookies.items():
        client.cookies.set(key, value)
    return client


def test_deleted_user_sessions_are_revoked_and_relogin_is_denied(env):
    app = env["app"]
    admin_cookies, admin_headers = _session_cookies(_login(app, "root", "root-password-1234"))
    admin = _api(app, admin_cookies, admin_headers)

    assert admin.post("/api/settings/users", json={
        "user": "tempmod", "role": "moderator", "brands": ["brand-a"], "password": "tempmod-password-1",
    }).status_code == 200
    assert admin.post("/api/settings/users", json={
        "user": "bystander", "role": "moderator", "brands": ["brand-b"], "password": "bystander-password-1",
    }).status_code == 200

    # Two independent sessions for the target user plus one for another user.
    s1_cookies, _ = _session_cookies(_login(app, "tempmod", "tempmod-password-1"))
    s2_cookies, _ = _session_cookies(_login(app, "tempmod", "tempmod-password-1"))
    other_cookies, _ = _session_cookies(_login(app, "bystander", "bystander-password-1"))

    assert _api(app, s1_cookies).get("/api/jobs").status_code == 200
    assert _api(app, s2_cookies).get("/api/jobs").status_code == 200
    assert _api(app, other_cookies).get("/api/jobs").status_code == 200

    # Delete target account.
    assert admin.delete("/api/settings/users/tempmod").status_code == 200

    # All prior sessions of the deleted user are immediately denied.
    for stale in (s1_cookies, s2_cookies):
        assert _api(app, stale).get("/api/session").status_code == 401
        assert _api(app, stale).get("/api/jobs").status_code == 401

    # Deleted account cannot log in again.
    assert _login(app, "tempmod", "tempmod-password-1").status_code == 401

    # Other users and the administrator keep working.
    assert _api(app, other_cookies).get("/api/jobs").status_code == 200
    assert admin.get("/api/settings/users").status_code == 200

    # Revocation store is persisted with strict file permissions.
    versions_path = env["config"] / "session-versions.json"
    assert versions_path.exists()
    assert stat.S_IMODE(versions_path.stat().st_mode) == 0o600

    # Even if an account with the same username is later recreated, the pre-deletion token stays invalid.
    assert admin.post("/api/settings/users", json={
        "user": "tempmod", "role": "moderator", "brands": ["brand-a"], "password": "recreated-password-1",
    }).status_code == 200
    assert _api(app, s1_cookies).get("/api/jobs").status_code == 401
    fresh_cookies, _ = _session_cookies(_login(app, "tempmod", "recreated-password-1"))
    assert _api(app, fresh_cookies).get("/api/jobs").status_code == 200


def test_role_demotion_revokes_prior_session_and_relogin_has_actual_permissions(env):
    app = env["app"]
    admin_cookies, admin_headers = _session_cookies(_login(app, "root", "root-password-1234"))
    admin = _api(app, admin_cookies, admin_headers)

    assert admin.post("/api/settings/users", json={
        "user": "alice", "role": "admin", "password": "alice-password-123",
    }).status_code == 200
    assert admin.post("/api/settings/users", json={
        "user": "bob", "role": "admin", "password": "bob-password-1234",
    }).status_code == 200

    alice_old_cookies, alice_old_headers = _session_cookies(_login(app, "alice", "alice-password-123"))
    bob_cookies, _ = _session_cookies(_login(app, "bob", "bob-password-1234"))

    assert _api(app, alice_old_cookies, alice_old_headers).get("/api/settings/users").status_code == 200

    # Demote alice from admin to moderator scoped only to brand-a.
    demoted = admin.put("/api/settings/users/alice", json={"role": "moderator", "brands": ["brand-a"]})
    assert demoted.status_code == 200
    assert demoted.json()["role"] == "moderator"
    assert demoted.json()["brands"] == ["brand-a"]

    # Prior admin session is revoked immediately.
    assert _api(app, alice_old_cookies, alice_old_headers).get("/api/settings/users").status_code == 401
    assert _api(app, alice_old_cookies).get("/api/jobs").status_code == 401
    assert _api(app, alice_old_cookies).get("/api/session").status_code == 401

    # Bob's session is unaffected.
    assert _api(app, bob_cookies).get("/api/settings/users").status_code == 200

    # New login gets only current moderator permissions.
    relogin = _login(app, "alice", "alice-password-123")
    assert relogin.status_code == 200
    assert relogin.json()["role"] == "moderator"
    assert relogin.json()["brands"] == ["brand-a"]
    alice_new_cookies, alice_new_headers = _session_cookies(relogin)
    alice_new = _api(app, alice_new_cookies, alice_new_headers)

    assert alice_new.get("/api/jobs").status_code == 200
    assert alice_new.get("/api/settings/users").status_code == 403
    foreign_job = alice_new.post("/api/jobs", json={"topic": "Тест", "brand_id": "brand-b"})
    assert foreign_job.status_code == 403


def test_self_service_password_change_revokes_all_prior_sessions_of_that_user(env):
    app = env["app"]
    admin_cookies, admin_headers = _session_cookies(_login(app, "root", "root-password-1234"))
    admin = _api(app, admin_cookies, admin_headers)

    assert admin.post("/api/settings/users", json={
        "user": "carol", "role": "moderator", "brands": ["brand-a"], "password": "carol-old-password",
    }).status_code == 200
    assert admin.post("/api/settings/users", json={
        "user": "dave", "role": "moderator", "brands": ["brand-a"], "password": "dave-password-123",
    }).status_code == 200

    c1_cookies, c1_headers = _session_cookies(_login(app, "carol", "carol-old-password"))
    c2_cookies, _ = _session_cookies(_login(app, "carol", "carol-old-password"))
    dave_cookies, _ = _session_cookies(_login(app, "dave", "dave-password-123"))

    # Failed password change (wrong current password) must NOT revoke the active session.
    bad = _api(app, c1_cookies, c1_headers).put("/api/session/password", json={
        "old_password": "wrong-password-123", "new_password": "carol-new-password",
    })
    assert bad.status_code == 400
    assert _api(app, c1_cookies).get("/api/jobs").status_code == 200
    assert _api(app, c2_cookies).get("/api/jobs").status_code == 200

    # Successful self-service password change revokes both prior sessions of carol.
    ok = _api(app, c1_cookies, c1_headers).put("/api/session/password", json={
        "old_password": "carol-old-password", "new_password": "carol-new-password",
    })
    assert ok.status_code == 200

    assert _api(app, c1_cookies).get("/api/jobs").status_code == 401
    assert _api(app, c2_cookies).get("/api/jobs").status_code == 401
    assert _api(app, c1_cookies).get("/api/session").status_code == 401

    # Old password no longer works; new password opens a working session.
    assert _login(app, "carol", "carol-old-password").status_code == 401
    fresh = _login(app, "carol", "carol-new-password")
    assert fresh.status_code == 200
    fresh_cookies, _ = _session_cookies(fresh)
    assert _api(app, fresh_cookies).get("/api/jobs").status_code == 200

    # Dave's session is unaffected.
    assert _api(app, dave_cookies).get("/api/jobs").status_code == 200


def test_admin_password_reset_revokes_target_user_sessions(env):
    app = env["app"]
    admin_cookies, admin_headers = _session_cookies(_login(app, "root", "root-password-1234"))
    admin = _api(app, admin_cookies, admin_headers)

    assert admin.post("/api/settings/users", json={
        "user": "erin", "role": "moderator", "brands": ["brand-a"], "password": "erin-old-password1",
    }).status_code == 200

    erin_cookies, _ = _session_cookies(_login(app, "erin", "erin-old-password1"))
    assert _api(app, erin_cookies).get("/api/jobs").status_code == 200

    # Updating only brands does not revoke the active session.
    assert admin.put("/api/settings/users/erin", json={"brands": ["brand-a", "brand-b"]}).status_code == 200
    assert _api(app, erin_cookies).get("/api/jobs").status_code == 200

    # Admin resets erin's password -> active session is revoked.
    assert admin.put("/api/settings/users/erin", json={"password": "erin-new-password2"}).status_code == 200
    assert _api(app, erin_cookies).get("/api/jobs").status_code == 401
    assert _login(app, "erin", "erin-old-password1").status_code == 401
    fresh_cookies, _ = _session_cookies(_login(app, "erin", "erin-new-password2"))
    assert _api(app, fresh_cookies).get("/api/jobs").status_code == 200


def test_first_run_admin_password_change_revokes_prior_session(tmp_path, monkeypatch):
    from core.app import app
    from core.first_run import password_hash

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("USERS_JSON", "{}")
    (config / "installation.json").write_text(
        json.dumps({
            "installation_id": "fr-556",
            "completed_at": "2026-01-01T00:00:00+00:00",
            "administrator": {
                "username": "owner",
                "password_hash": password_hash("owner-initial-pass"),
                "role": "admin",
            },
        }),
        encoding="utf-8",
    )

    s1_cookies, s1_headers = _session_cookies(_login(app, "owner", "owner-initial-pass"))
    s2_cookies, _ = _session_cookies(_login(app, "owner", "owner-initial-pass"))
    assert _api(app, s1_cookies).get("/api/jobs").status_code == 200
    assert _api(app, s2_cookies).get("/api/jobs").status_code == 200

    changed = _api(app, s1_cookies, s1_headers).put("/api/session/password", json={
        "old_password": "owner-initial-pass", "new_password": "owner-rotated-pass",
    })
    assert changed.status_code == 200

    assert _api(app, s1_cookies).get("/api/jobs").status_code == 401
    assert _api(app, s2_cookies).get("/api/jobs").status_code == 401
    assert _login(app, "owner", "owner-initial-pass").status_code == 401
    fresh_cookies, _ = _session_cookies(_login(app, "owner", "owner-rotated-pass"))
    assert _api(app, fresh_cookies).get("/api/jobs").status_code == 200


def test_logout_and_revocation_persist_across_core_restart(env):
    app = env["app"]
    admin_cookies, admin_headers = _session_cookies(_login(app, "root", "root-password-1234"))
    admin = _api(app, admin_cookies, admin_headers)

    for user, role, pwd in (
        ("deleted_u", "moderator", "deleted-password-1"),
        ("demoted_u", "admin", "demoted-password-1"),
        ("pwreset_u", "moderator", "pwreset-password-1"),
        ("active_u", "moderator", "active-password-12"),
    ):
        payload = {"user": user, "role": role, "password": pwd}
        if role == "moderator":
            payload["brands"] = ["brand-a"]
        assert admin.post("/api/settings/users", json=payload).status_code == 200

    deleted_tok = _login(app, "deleted_u", "deleted-password-1").cookies["vertep_session"]
    demoted_tok = _login(app, "demoted_u", "demoted-password-1").cookies["vertep_session"]
    pwreset_tok = _login(app, "pwreset_u", "pwreset-password-1").cookies["vertep_session"]
    active_s1_cookies, active_s1_headers = _session_cookies(_login(app, "active_u", "active-password-12"))
    active_s2_tok = _login(app, "active_u", "active-password-12").cookies["vertep_session"]

    # Standard logout revokes only active_s1, keeping active_s2 alive.
    assert _api(app, active_s1_cookies, active_s1_headers).delete("/api/session").status_code == 200
    assert _api(app, active_s1_cookies).get("/api/jobs").status_code == 401
    assert _api(app, {"vertep_session": active_s2_tok}).get("/api/jobs").status_code == 200

    # Trigger all three user-wide revocations.
    assert admin.delete("/api/settings/users/deleted_u").status_code == 200
    assert admin.put("/api/settings/users/demoted_u", json={"role": "moderator", "brands": ["brand-a"]}).status_code == 200
    assert admin.put("/api/settings/users/pwreset_u", json={"password": "pwreset-password-2"}).status_code == 200

    demoted_fresh_tok = _login(app, "demoted_u", "demoted-password-1").cookies["vertep_session"]
    pwreset_fresh_tok = _login(app, "pwreset_u", "pwreset-password-2").cookies["vertep_session"]

    # Simulate CORE restart in a brand-new Python process pointing at the same CONFIG_ROOT.
    probe = """
import json, sys
from core.security import _valid_session
tokens = json.loads(sys.argv[1])
result = {name: _valid_session(tok) for name, tok in tokens.items()}
print(json.dumps(result))
"""
    sub_env = {
        **os.environ,
        "CONFIG_ROOT": str(env["config"]),
        "BRANDS_ROOT": str(env["brands"]),
        "ADMIN_USER": "root",
        "ADMIN_PASSWORD": "root-password-1234",
        "USERS_JSON": "{}",
    }
    payload = {
        "deleted_old": deleted_tok,
        "demoted_old": demoted_tok,
        "pwreset_old": pwreset_tok,
        "logged_out": active_s1_cookies["vertep_session"],
        "active_other": active_s2_tok,
        "demoted_fresh": demoted_fresh_tok,
        "pwreset_fresh": pwreset_fresh_tok,
    }
    proc = subprocess.run(
        [sys.executable, "-c", probe, json.dumps(payload)],
        env=sub_env,
        capture_output=True,
        text=True,
        check=True,
    )
    after_restart = json.loads(proc.stdout)
    assert after_restart["deleted_old"] is None
    assert after_restart["demoted_old"] is None
    assert after_restart["pwreset_old"] is None
    assert after_restart["logged_out"] is None
    assert after_restart["active_other"] == ["active_u", "moderator"]
    assert after_restart["demoted_fresh"] == ["demoted_u", "moderator"]
    assert after_restart["pwreset_fresh"] == ["pwreset_u", "moderator"]

    # Also verify module reload in-process preserves revocation state.
    import core.security as security_mod
    importlib.reload(security_mod)
    assert security_mod._valid_session(deleted_tok) is None
    assert security_mod._valid_session(demoted_tok) is None
    assert security_mod._valid_session(pwreset_tok) is None
    assert security_mod._valid_session(active_s2_tok) == ("active_u", "moderator")


def test_legacy_four_part_token_is_revoked_when_user_version_bumps(env):
    from core.first_run import session_secret
    from core.security import _valid_session, revoke_user_sessions

    expiry = str(int(time.time()) + 3600)
    legacy_payload = f"{expiry}:legacyuser:moderator:deadbeef"
    sig = hmac.new(session_secret().encode(), legacy_payload.encode(), hashlib.sha256).hexdigest()
    legacy_token = f"{legacy_payload}.{sig}"

    assert _valid_session(legacy_token) == ("legacyuser", "moderator")
    revoke_user_sessions("legacyuser")
    assert _valid_session(legacy_token) is None
