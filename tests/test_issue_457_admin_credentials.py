"""#457: Security reports the administrator account authentication actually uses."""
import json

import pytest

from core import security
from core.api.observability import _effective_config_summary
from core.first_run import password_hash


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    for name in ("ADMIN_PASSWORD", "ADMIN_USER", "USERS_JSON"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _install(root, administrator):
    (root / "installation.json").write_text(json.dumps({"completed_at": "now", "administrator": administrator}))


def test_first_run_administrator_without_env_is_configured(config):
    secret = "first-run-password-123"
    _install(config, {"username": "owner", "password_hash": password_hash(secret)})
    summary = _effective_config_summary()
    assert summary["admin_password_set"] is True
    assert summary["admin_credentials"] == {"state": "configured", "sources": ["first_run"], "problems": []}
    # The indicator agrees with the authentication path it describes.
    assert security._authenticate_user("owner", secret) == "admin"
    assert secret not in json.dumps(summary) and "pbkdf2" not in json.dumps(summary)


def test_env_only_fallback_is_reported_as_its_own_source(config, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "env-only-password-1")
    status = security.admin_credentials_status()
    assert status == {"state": "configured", "sources": ["environment"], "problems": []}
    assert security._authenticate_user("admin", "env-only-password-1") == "admin"


def test_missing_credentials_are_not_reported_as_set(config):
    (config / "users.json").write_text(json.dumps({"viewer": {"password_hash": password_hash("viewer-password-1"), "role": "viewer"}}))
    summary = _effective_config_summary()
    # A user that exists but is not an administrator does not count.
    assert summary["admin_password_set"] is False
    assert summary["admin_credentials"]["state"] == "missing"


@pytest.mark.parametrize("broken", [{"username": "owner", "password_hash": ""},
                                    {"username": "owner", "password_hash": "md5$abc"},
                                    {"username": "", "password_hash": password_hash("x" * 12)},
                                    "not-a-record"])
def test_damaged_first_run_administrator_is_invalid_not_configured(config, broken):
    _install(config, broken)
    status = security.admin_credentials_status()
    assert status["state"] == "invalid" and status["problems"] == ["first_run_invalid"]
    assert _effective_config_summary()["admin_password_set"] is False


def test_admin_in_the_user_store_counts_only_with_a_usable_hash(config, monkeypatch):
    monkeypatch.setenv("USERS_JSON", json.dumps({"ops": {"password_hash": password_hash("ops-password-123"), "role": "admin"},
                                                 "old": {"password_hash": "plain-text", "role": "admin"}}))
    status = security.admin_credentials_status()
    assert status["state"] == "configured" and status["sources"] == ["user_store"]
    assert status["problems"] == ["user_store_invalid"]


def test_password_change_and_restart_keep_the_indicator_consistent(config):
    _install(config, {"username": "owner", "password_hash": password_hash("first-run-password-123")})
    assert security.set_account_password("owner", "changed-password-456")
    # A restart re-reads the persisted installation: nothing is cached in the process.
    assert security.admin_credentials_status()["state"] == "configured"
    assert security._authenticate_user("owner", "changed-password-456") == "admin"
    assert security._authenticate_user("owner", "first-run-password-123") is None
