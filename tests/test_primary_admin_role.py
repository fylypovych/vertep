"""Основний адміністратор зберігає права при застарілій ролі у записі/сесії."""
import pytest

from core import security


@pytest.fixture
def accounts(monkeypatch):
    monkeypatch.setattr(security, "configured_user", lambda: None)
    monkeypatch.setattr(security, "session_secret", lambda: "isolated-session-signing-secret")
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-primary-password")
    monkeypatch.setattr(security, "_load_all_users", lambda: {})


def test_persisted_viewer_role_does_not_demote_primary_admin(accounts, monkeypatch):
    encoded = security._hash_secret("changed-password")
    monkeypatch.setattr(security, "_load_all_users", lambda: {
        "admin": {"password_hash": encoded, "role": "viewer"},
    })
    assert security._authenticate_user("admin", "changed-password") == "admin"
    assert security._authenticate_user("admin", "incorrect") is None


def test_wizard_administrator_is_admin_even_with_old_viewer_record(accounts, monkeypatch):
    encoded = security._hash_secret("wizard-password")
    monkeypatch.setattr(security, "configured_user", lambda: (
        "owner", {"password_hash": encoded, "role": "viewer"},
    ))
    assert security._authenticate_user("owner", "wizard-password") == "admin"
    assert security._valid_session(security._session_token("owner", "viewer")) == ("owner", "admin")


def test_existing_primary_admin_session_recovers_role(accounts):
    token = security._session_token("admin", "viewer")
    assert security._valid_session(token) == ("admin", "admin")
    assert security._valid_session(token + "tampered") is None


def test_ordinary_viewer_keeps_role(accounts, monkeypatch):
    encoded = security._hash_secret("viewer-password")
    monkeypatch.setattr(security, "_load_all_users", lambda: {
        "reader": {"password_hash": encoded, "role": "viewer"},
    })
    assert security._authenticate_user("reader", "viewer-password") == "viewer"
    assert security._valid_session(security._session_token("reader", "viewer")) == ("reader", "viewer")


def test_name_admin_alone_does_not_grant_privileges(accounts, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD")
    assert security._valid_session(security._session_token("admin", "viewer")) == ("admin", "viewer")


def test_expired_primary_admin_session_is_rejected(accounts, monkeypatch):
    monkeypatch.setenv("SESSION_TTL", "-1")
    assert security._valid_session(security._session_token("admin", "viewer")) is None
