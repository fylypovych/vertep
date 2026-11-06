"""i.0.0.4.12 against an actual PostgreSQL: the migration file, the row and the file removal."""
import os
from pathlib import Path

import pytest

from core import first_run

SECRET = "real-postgres-secret-value"


@pytest.fixture
def database(monkeypatch, tmp_path):
    url = os.getenv("VERTEP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("VERTEP_TEST_DATABASE_URL is not set")
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(url) as connection:
        connection.execute("DROP TABLE IF EXISTS secret_store")
        connection.execute(Path("db/013_secret_store.sql").read_text(encoding="utf-8"))
    monkeypatch.setattr(first_run, "config_root", lambda: tmp_path)
    monkeypatch.setenv("STORAGE_BACKEND", "file")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    yield url
    with psycopg.connect(url) as connection:
        connection.execute("DROP TABLE IF EXISTS secret_store")


def _row(url):
    import psycopg
    with psycopg.connect(url) as connection:
        return connection.execute("SELECT envelope::text, count(*) OVER () FROM secret_store").fetchall()


def test_file_store_moves_into_postgres_and_is_removed(database, tmp_path, monkeypatch):
    first_run.set_integration_secret("youtube_access_token", SECRET)
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", database)

    assert first_run.migrate_secret_file_to_database() is True
    assert not (tmp_path / "secrets.enc.json").exists()
    rows = _row(database)
    assert len(rows) == 1 and SECRET not in rows[0][0]
    assert first_run.ensure_secret_store()["youtube_access_token"] == SECRET
    assert first_run.migrate_secret_file_to_database() is False


def test_updates_stay_one_encrypted_row(database, tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", database)
    first_run.set_integration_secret("youtube_access_token", SECRET)
    first_run.set_integration_secret("facebook_access_token", "second-value")
    rows = _row(database)
    assert len(rows) == 1 and SECRET not in rows[0][0]
    stored = first_run.ensure_secret_store()
    assert stored["youtube_access_token"] == SECRET and stored["facebook_access_token"] == "second-value"
    assert not (tmp_path / "secrets.enc.json").exists()
