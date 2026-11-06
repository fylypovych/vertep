"""i.0.0.4.12: the secret store lives in PostgreSQL; nodes receive secrets through CORE."""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from core import first_run
from core.api import tasks
from publishers import oauth
from publishers.task_secrets import TaskSecrets, task_secrets


@pytest.fixture
def database(monkeypatch, tmp_path):
    """The row is one JSON value, so a dict stands in for the table."""
    table = {}
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setattr(first_run, "config_root", lambda: tmp_path)
    monkeypatch.setattr(first_run, "_database_envelope", lambda dsn: table.get("row"))
    monkeypatch.setattr(first_run, "_write_database_envelope",
                        lambda dsn, envelope: table.__setitem__("row", json.loads(json.dumps(envelope))))
    return table


def _use_database(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "host=postgres")


def _file_store(monkeypatch, value):
    monkeypatch.setenv("STORAGE_BACKEND", "file")
    first_run._write_encrypted_secrets(value)
    _use_database(monkeypatch)


def test_new_store_goes_to_the_database_encrypted(database, tmp_path, monkeypatch):
    _use_database(monkeypatch)
    first_run.set_integration_secret("telegram_bot_token", "very-secret")
    assert not (tmp_path / "secrets.enc.json").exists()
    assert "very-secret" not in json.dumps(database["row"])
    assert first_run.ensure_secret_store()["telegram_bot_token"] == "very-secret"


def test_file_moves_to_the_database_once_and_is_removed(database, tmp_path, monkeypatch):
    _file_store(monkeypatch, {"telegram_bot_token": "t", "jwt_secret": "j"})
    assert first_run.migrate_secret_file_to_database() is True
    assert not (tmp_path / "secrets.enc.json").exists()
    row = dict(database["row"])
    assert first_run.migrate_secret_file_to_database() is False
    assert database["row"] == row
    assert first_run.ensure_secret_store()["jwt_secret"] == "j"


def test_failed_verification_keeps_the_file(database, tmp_path, monkeypatch):
    _file_store(monkeypatch, {"telegram_bot_token": "t"})
    monkeypatch.setattr(first_run, "_write_database_envelope",
                        lambda dsn, envelope: database.__setitem__("row", {**envelope, "ciphertext": "AAAA"}))
    with pytest.raises(ValueError):
        first_run.migrate_secret_file_to_database()
    assert (tmp_path / "secrets.enc.json").exists()


def test_corrupt_database_row_keeps_the_file_and_is_not_replaced(database, tmp_path, monkeypatch):
    _file_store(monkeypatch, {"telegram_bot_token": "t"})
    database["row"] = {"version": 1, "algorithm": "AES-256-GCM", "nonce": "AAAA", "ciphertext": "AAAA"}
    with pytest.raises(ValueError):
        first_run.ensure_secret_store()
    assert (tmp_path / "secrets.enc.json").exists()
    assert database["row"]["ciphertext"] == "AAAA"


def test_unreachable_database_never_creates_a_new_store(database, monkeypatch):
    _use_database(monkeypatch)

    def down(dsn):
        raise OSError("down")

    monkeypatch.setattr(first_run, "_database_envelope", down)
    with pytest.raises(OSError):
        first_run.ensure_secret_store()
    assert "row" not in database


def test_key_stays_out_of_the_database(database, tmp_path, monkeypatch):
    _use_database(monkeypatch)
    first_run.ensure_secret_store()
    assert (tmp_path / "secret-store.key").exists() or "SECRET_STORE_PASSPHRASE" in str(database)
    assert set(database["row"]) == {"version", "algorithm", "nonce", "ciphertext"}


@pytest.fixture
def owned_task(database, monkeypatch):
    _use_database(monkeypatch)
    first_run.set_integration_secret("youtube_access_token", "yt")
    first_run.set_integration_secret("facebook_access_token", "fb")
    job = SimpleNamespace(publish_task_ids={"t1": "youtube"})
    monkeypatch.setattr(tasks.store, "jobs", {"j1": job})
    monkeypatch.setattr(tasks.store, "workers", {"n1": {"current_job": "j1", "current_task": "t1"}})
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, request: True)
    return SimpleNamespace(headers={"x-vertep-node": "n1"})


def test_node_receives_only_its_channel(owned_task):
    answer = tasks.task_secrets("t1", owned_task)
    assert answer["secrets"] == {"youtube_access_token": "yt"}


def test_other_task_is_refused(owned_task):
    with pytest.raises(HTTPException) as error:
        tasks.task_secrets("t2", owned_task)
    assert error.value.status_code == 409


def test_invalid_node_is_refused(owned_task, monkeypatch):
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, request: False)
    with pytest.raises(HTTPException) as error:
        tasks.task_secrets("t1", owned_task)
    assert error.value.status_code == 401


def test_node_cannot_overwrite_another_channel(owned_task):
    with pytest.raises(HTTPException) as error:
        tasks.save_task_secrets("t1", {"secrets": {"facebook_access_token": "x"}}, owned_task)
    assert error.value.status_code == 403
    assert first_run.ensure_secret_store()["facebook_access_token"] == "fb"


def test_refreshed_token_returns_to_the_store(owned_task):
    tasks.save_task_secrets("t1", {"secrets": {"youtube_access_token": "new"}}, owned_task)
    assert first_run.ensure_secret_store()["youtube_access_token"] == "new"


def test_publisher_reads_through_core_during_a_task(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(url)
        return httpx.Response(200, json={"secrets": {"telegram_bot_token": "from-core"}},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr("publishers.task_secrets.httpx.get", fake_get)
    with task_secrets(TaskSecrets("https://core/api/tasks/t/secrets", {})):
        assert oauth._get_secret("TELEGRAM_BOT_TOKEN") == "from-core"
        assert oauth._get_secret("TELEGRAM_BOT_TOKEN") == "from-core"
    assert len(calls) == 1
