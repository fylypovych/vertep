import json

import pytest

from core.first_run import (complete_setup, configured_user, ensure_secret_store,
                            integration_secret_status, is_configured,
                            set_integration_secret, setup_status)


def test_first_run_creates_manifest_and_secret_store(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("VERTEP_VERSION", "1.2.3")
    assert not is_configured()
    status = setup_status()
    assert status["configured"] is False
    assert (tmp_path / "secrets.enc.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "secret-store.key").stat().st_mode & 0o777 == 0o600
    result = complete_setup("Vertep Production", "operator", "very-secure-password",
                            "very-secure-password", "ollama")
    assert result["version"] == "1.2.3"
    assert result["node_role"] == "core"
    assert is_configured()
    username, record = configured_user()
    assert username == "operator"
    assert "very-secure-password" not in json.dumps(record)
    assert record["password_hash"].startswith("pbkdf2_sha256$")


def test_secret_store_encrypts_migrates_and_detects_tampering(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    legacy = {"session_secret": "legacy-secret", "encryption_key": "obsolete"}
    (tmp_path / "bootstrap-secrets.json").write_text(json.dumps(legacy), encoding="utf-8")

    stored = ensure_secret_store()
    envelope_path = tmp_path / "secrets.enc.json"
    envelope_text = envelope_path.read_text(encoding="utf-8")
    assert stored["session_secret"] == "legacy-secret"
    assert "encryption_key" not in stored
    assert "legacy-secret" not in envelope_text
    assert not (tmp_path / "bootstrap-secrets.json").exists()

    envelope = json.loads(envelope_text)
    envelope["ciphertext"] = envelope["ciphertext"][:-4] + "AAAA"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError, match="authentication"):
        ensure_secret_store()


def test_first_run_is_single_use_and_validates_input(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="12 characters"):
        complete_setup("Production", "admin", "short", "short", "skip")
    complete_setup("Production", "admin", "a-secure-password", "a-secure-password", "skip")
    with pytest.raises(FileExistsError):
        complete_setup("Other", "other", "another-password", "another-password", "skip")


def test_integration_secrets_are_write_only_and_removable(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    secret = "telegram-secret-value"
    status = set_integration_secret("telegram_bot_token", secret)
    assert status["telegram_bot_token"] is True
    assert integration_secret_status()["telegram_bot_token"] is True
    assert secret not in (tmp_path / "secrets.enc.json").read_text(encoding="utf-8")
    status = set_integration_secret("telegram_bot_token", None)
    assert status["telegram_bot_token"] is False
    with pytest.raises(ValueError, match="Unsupported"):
        set_integration_secret("arbitrary_secret", "value")


def test_data_key_is_sealed_and_wrong_passphrase_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "correct horse battery staple")
    ensure_secret_store()
    key_envelope = json.loads((tmp_path / "secret-store.key").read_text())
    assert key_envelope["algorithm"] == "scrypt+A256GCM"
    assert "correct horse" not in json.dumps(key_envelope)
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "wrong passphrase value")
    with pytest.raises(ValueError, match="authentication"):
        ensure_secret_store()
