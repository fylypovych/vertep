import json

import pytest

from core.first_run import complete_setup, configured_user, is_configured, setup_status


def test_first_run_creates_manifest_and_secret_store(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("VERTEP_VERSION", "1.2.3")
    assert not is_configured()
    status = setup_status()
    assert status["configured"] is False
    assert (tmp_path / "secrets.json").stat().st_mode & 0o777 == 0o600
    result = complete_setup("Vertep Production", "operator", "very-secure-password",
                            "very-secure-password", "ollama")
    assert result["version"] == "1.2.3"
    assert result["node_role"] == "core"
    assert is_configured()
    username, record = configured_user()
    assert username == "operator"
    assert "very-secure-password" not in json.dumps(record)
    assert record["password_hash"].startswith("pbkdf2_sha256$")


def test_first_run_is_single_use_and_validates_input(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="12 characters"):
        complete_setup("Production", "admin", "short", "short", "skip")
    complete_setup("Production", "admin", "a-secure-password", "a-secure-password", "skip")
    with pytest.raises(FileExistsError):
        complete_setup("Other", "other", "another-password", "another-password", "skip")
