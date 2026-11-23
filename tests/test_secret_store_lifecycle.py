"""Secret-store key lifecycle contract (Issue #84).

The sealed data key must survive rotation, restart, concurrent access and an
interrupted rotation, and must fail closed when the passphrase is missing or
wrong.  These tests use a disposable ``tmp_path`` config root only — no
production storage is touched.
"""

import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from core import first_run


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "p" * 32)
    monkeypatch.delenv("SECRET_STORE_PASSPHRASE_FILE", raising=False)
    monkeypatch.delenv("REQUIRE_SECRET_KEY_SEALING", raising=False)
    return tmp_path


def key_file(root: Path) -> Path:
    return root / "secret-store.key"


def test_rotation_re_seals_under_a_new_data_key(store):
    first_run.ensure_secret_store()
    first_run.set_integration_secret("telegram_bot_token", "token-before")
    before = key_file(store).read_text(encoding="ascii")

    result = first_run.rotate_data_key()

    assert result["rotated"] is True
    assert key_file(store).read_text(encoding="ascii") != before
    # The payload survives rotation: only the wrapping key changed.
    assert first_run.ensure_secret_store()["telegram_bot_token"] == "token-before"


def test_rotation_keeps_a_backup_key_and_the_retired_key_no_longer_opens_the_store(store):
    first_run.ensure_secret_store()
    first_run.set_integration_secret("smtp_password", "mail-pass-1")
    backup_before = (store / "secret-store.key.prev")
    backup_before.unlink(missing_ok=True)

    first_run.rotate_data_key()
    rotated_key = key_file(store).read_text(encoding="ascii")
    assert backup_before.exists()

    # Restore the previous key file the way a failed rotation would.
    shutil.copyfile(backup_before, key_file(store))
    assert key_file(store).read_text(encoding="ascii") != rotated_key
    # The store is not readable with the retired key — rotation was effective.
    with pytest.raises(ValueError):
        first_run.ensure_secret_store()


def test_interrupted_rotation_restores_the_readable_key(store, monkeypatch):
    """A crash between key replacement and rewrite must not strand the store."""
    first_run.ensure_secret_store()
    first_run.set_integration_secret("smtp_password", "mail-pass-1")
    original = key_file(store).read_bytes()

    def boom(_value):
        raise OSError("simulated crash during rewrite")

    patched = first_run._write_encrypted_secrets
    monkeypatch.setattr(first_run, "_write_encrypted_secrets", boom)
    with pytest.raises(OSError):
        first_run.rotate_data_key()

    assert key_file(store).read_bytes() == original
    # Restore only the writer; undoing the whole fixture would drop CONFIG_ROOT.
    first_run._write_encrypted_secrets = patched
    assert first_run.ensure_secret_store()["smtp_password"] == "mail-pass-1"


def test_a_deleted_key_file_fails_closed_instead_of_returning_an_empty_store(store):
    first_run.ensure_secret_store()
    first_run.set_integration_secret("telegram_bot_token", "token-value")
    key_file(store).unlink()

    # A deleted data key is unrecoverable by definition; the store must fail
    # closed instead of silently returning an empty configuration.
    with pytest.raises(ValueError):
        first_run.ensure_secret_store()


def test_wrong_passphrase_fails_closed_and_reports_effective_state(store, monkeypatch):
    first_run.ensure_secret_store()
    first_run.set_integration_secret("smtp_password", "mail-pass-1")

    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "w" * 32)
    with pytest.raises(ValueError):
        first_run.ensure_secret_store()

    state = first_run.inspect_data_key()
    assert state["sealed"] is True
    assert state["unsealable"] is False


def test_missing_passphrase_fails_closed_when_sealing_is_required(store, monkeypatch):
    first_run.ensure_secret_store()
    monkeypatch.delenv("SECRET_STORE_PASSPHRASE", raising=False)
    monkeypatch.setenv("REQUIRE_SECRET_KEY_SEALING", "true")

    with pytest.raises((RuntimeError, ValueError)):
        first_run.ensure_secret_store()
    assert first_run.inspect_data_key()["unsealable"] is False


def test_sealed_store_survives_a_process_restart(store):
    first_run.ensure_secret_store()
    first_run.set_integration_secret("telegram_bot_token", "token-value")

    # The value must come from the on-disk envelope, not from any module state:
    # a new process with the same configuration reads it back.
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    reloaded = subprocess.run(
        [sys.executable, "-c", "from core import first_run; print(first_run.ensure_secret_store()['telegram_bot_token'])"],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True, timeout=60)
    assert reloaded.returncode == 0, reloaded.stderr
    assert reloaded.stdout.strip() == "token-value"
    assert first_run.inspect_data_key()["sealed"] is True


def test_concurrent_writers_do_not_lose_or_corrupt_the_store(store):
    first_run.ensure_secret_store()
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def writer(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            first_run.set_integration_secret("smtp_password", f"pass-{index}")
        except Exception as error:  # pragma: no cover - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    stored = first_run.ensure_secret_store()
    assert stored["smtp_password"] in {f"pass-{index}" for index in range(4)}
    envelope = json.loads((store / "secrets.enc.json").read_text(encoding="utf-8"))
    assert envelope["algorithm"] == "AES-256-GCM"


def test_backup_and_restore_round_trip_on_disposable_storage(store, tmp_path):
    """A full config-root copy/restore round trip keeps the store readable."""
    first_run.ensure_secret_store()
    first_run.set_integration_secret("telegram_bot_token", "token-value")

    archive = tmp_path / "backup-copy"
    shutil.copytree(store, archive)

    (store / "secrets.enc.json").unlink()
    (store / "secret-store.key").unlink()
    first_run.ensure_secret_store()
    assert first_run.ensure_secret_store().get("telegram_bot_token") is None

    for name in ("secrets.enc.json", "secret-store.key"):
        shutil.copyfile(archive / name, store / name)
    os.chmod(store / "secret-store.key", 0o600)

    assert first_run.ensure_secret_store()["telegram_bot_token"] == "token-value"


def test_key_file_permissions_are_owner_only(store):
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not enforced on Windows")
    first_run.ensure_secret_store()
    assert first_run._secret_key()  # forces the sealed key file to exist
    assert key_file(store).stat().st_mode & 0o077 == 0

def test_a_process_that_loses_the_key_creation_race_keeps_the_winners_key(store, monkeypatch):
    """#520: the loser of os.link must read the winner's key, never delete it."""
    import base64
    winner = os.urandom(32)
    path = key_file(store)

    def winner_links_first(source, destination):
        first_run._write_sealed_key(destination, winner)  # the other process finished first
        raise FileExistsError(destination)
    assert not path.exists()
    with monkeypatch.context() as race:
        race.setattr(first_run.os, "link", winner_links_first)
        assert first_run._secret_key() == winner
    assert first_run._secret_key() == winner  # the winner's key is still the one on disk
    assert list(store.glob(".secret-store.key.*.tmp")) == []
