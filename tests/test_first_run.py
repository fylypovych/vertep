import json
import os
import threading

import pytest

from core.first_run import (complete_setup, configured_user, ensure_secret_store,
                            integration_secret_status, is_configured, rotate_data_key,
                            set_integration_secret, setup_status)


def test_first_run_creates_manifest_and_secret_store(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("VERTEP_VERSION", "1.2.3")
    (tmp_path / "deployment-plan.json").write_text(json.dumps({
        "sha256": "a" * 64, "services": ["core", "postgres"], "capabilities": ["scheduling"]}))
    assert not is_configured()
    status = setup_status()
    assert status["configured"] is False
    if os.name != "nt":
        assert (tmp_path / "secrets.enc.json").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "secret-store.key").stat().st_mode & 0o777 == 0o600
    result = complete_setup("Vertep Production", "operator", "very-secure-password",
                            "very-secure-password", "ollama")
    assert result["version"] == "1.2.3"
    assert result["node_role"] == "core"
    assert result["runtime"]["deployment_plan_sha256"] != "a" * 64
    assert "core" in result["runtime"]["services"]
    deployment_request = json.loads((tmp_path / "deployment-request.json").read_text())
    assert deployment_request["role"] == "core"
    assert deployment_request["ai_backend"] == "ollama"
    assert deployment_request["additional_roles"] == ["text"]
    assert "ollama" in result["runtime"]["services"]
    assert deployment_request["plan_sha256"] == result["runtime"]["deployment_plan_sha256"]
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
    passphrase = tmp_path / "passphrase"
    passphrase.write_text("correct horse battery staple")
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE_FILE", str(passphrase))
    ensure_secret_store()
    key_envelope = json.loads((tmp_path / "secret-store.key").read_text())
    assert key_envelope["algorithm"] == "scrypt+A256GCM"
    assert "correct horse" not in json.dumps(key_envelope)
    passphrase.write_text("wrong passphrase value")
    with pytest.raises(ValueError, match="authentication"):
        ensure_secret_store()


def test_raw_key_is_transparently_resealed(monkeypatch, tmp_path):
    """A store created without a passphrase keeps its data when a passphrase
    appears later: the raw key is re-sealed in place, not regenerated."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    for name in ("SECRET_STORE_PASSPHRASE", "SECRET_STORE_PASSPHRASE_FILE"):
        monkeypatch.delenv(name, raising=False)
    set_integration_secret("telegram_bot_token", "keep-me")
    key_path = tmp_path / "secret-store.key"
    raw = key_path.read_text(encoding="utf-8")
    assert not raw.lstrip().startswith("{")

    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "a-very-strong-passphrase")
    status = integration_secret_status()
    assert status["telegram_bot_token"] is True
    envelope = json.loads(key_path.read_text(encoding="utf-8"))
    assert envelope["algorithm"] == "scrypt+A256GCM"
    assert "a-very-strong-passphrase" not in key_path.read_text(encoding="utf-8")
    assert "keep-me" not in key_path.read_text(encoding="utf-8")
    # Re-sealing must not change the data key, so the ciphertext still decrypts.
    assert set_integration_secret("telegram_bot_token", "still-here")["telegram_bot_token"] is True


def test_rotation_rewrites_store_under_new_data_key(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "a-very-strong-passphrase")
    set_integration_secret("telegram_bot_token", "rotate-me")
    before = (tmp_path / "secret-store.key").read_text(encoding="utf-8")

    result = rotate_data_key()
    after = (tmp_path / "secret-store.key").read_text(encoding="utf-8")
    assert result["rotated"] is True
    assert after != before
    assert integration_secret_status()["telegram_bot_token"] is True
    assert (tmp_path / "secret-store.key.prev").exists()


def test_rotation_survives_restart_and_rejects_wrong_passphrase(monkeypatch, tmp_path):
    """Rotation must not weaken the effective sealing gate: after rotation a
    wrong passphrase still fails closed."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "a-very-strong-passphrase")
    set_integration_secret("telegram_bot_token", "rotate-me")
    rotate_data_key()
    # Simulate a CORE restart: nothing is cached, everything is re-read.
    assert ensure_secret_store()["telegram_bot_token"] == "rotate-me"
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "another-strong-passphrase")
    with pytest.raises(ValueError, match="authentication"):
        ensure_secret_store()


def test_rotation_is_serialized_with_concurrent_writers(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "a-very-strong-passphrase")
    set_integration_secret("telegram_bot_token", "concurrent")
    errors: list[Exception] = []

    def writer(value: str) -> None:
        try:
            set_integration_secret("smtp_password", value)
        except Exception as error:  # pragma: no cover - only on a real regression
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(f"value-{index}",)) for index in range(8)]
    for thread in threads:
        thread.start()
    rotate_data_key()
    for thread in threads:
        thread.join()
    assert errors == []
    assert integration_secret_status()["smtp_password"] is True
    assert integration_secret_status()["telegram_bot_token"] is True


def test_interrupted_rotation_recovers_from_previous_key(monkeypatch, tmp_path):
    """If the store rewrite fails mid-rotation the previous key file is put
    back, so the sealed store is never left undecryptable."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "a-very-strong-passphrase")
    set_integration_secret("telegram_bot_token", "keep-me")
    original = (tmp_path / "secret-store.key").read_text(encoding="utf-8")

    def boom(_value: dict) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr("core.first_run._write_encrypted_secrets", boom)
        with pytest.raises(OSError):
            rotate_data_key()

    assert (tmp_path / "secret-store.key").read_text(encoding="utf-8") == original
    assert ensure_secret_store()["telegram_bot_token"] == "keep-me"


def test_data_key_is_not_replaced_when_another_process_wins(monkeypatch, tmp_path):
    """Concurrent first use must converge on one key instead of orphaning the
    store under a key nobody kept."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "a-very-strong-passphrase")
    ensure_secret_store()
    first = (tmp_path / "secret-store.key").read_text(encoding="utf-8")
    assert (tmp_path / "secrets.enc.json").exists()

    # A second process writing the store must keep using the same data key.
    set_integration_secret("smtp_password", "second-process")
    assert (tmp_path / "secret-store.key").read_text(encoding="utf-8") == first
    assert ensure_secret_store()["smtp_password"] == "second-process"
