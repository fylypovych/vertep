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


def test_row_survives_pg_dump_and_restore(database, monkeypatch):
    """The real backup path: the encrypted row returns with the dump and still opens."""
    import shutil
    import subprocess
    tools = os.getenv("VERTEP_TEST_POSTGRES_BIN", "")
    dump = shutil.which("pg_dump", path=tools or None)
    restore = shutil.which("pg_restore", path=tools or None)
    if not dump or not restore:
        pytest.skip("pg_dump/pg_restore are unavailable")
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", database)
    first_run.set_integration_secret("youtube_access_token", SECRET)
    archive = subprocess.run([dump, "-Fc", "-t", "secret_store", database], capture_output=True, check=True).stdout
    first_run.set_integration_secret("youtube_access_token", "changed-after-backup")
    subprocess.run([restore, "--clean", "--if-exists", "-d", database], input=archive, check=True, capture_output=True)
    assert first_run.ensure_secret_store()["youtube_access_token"] == SECRET


def test_startup_recovery_reads_the_real_database_state(database, monkeypatch, tmp_path):
    """i.0.0.4.5: the record CORE reads in production, in the bootstrap configuration."""
    import importlib.util
    import psycopg
    from core.system_state import _database_state, set_system_state
    with psycopg.connect(database) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS system_operating_state(
            singleton BOOLEAN PRIMARY KEY, state TEXT NOT NULL, reason TEXT,
            operation_id TEXT, updated_at TIMESTAMPTZ NOT NULL)""")
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("UPDATE_DATABASE_URL", database)
    spec = importlib.util.spec_from_file_location("startup_recovery_real", "scripts/startup-recovery.py")
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)
    try:
        set_system_state("MAINTENANCE", "restore", "restore:snap", tmp_path)
        recovery.reconcile_database_state(tmp_path, attempts=1)
        assert _database_state()["state"] == "MAINTENANCE"
        set_system_state("UPDATING", "update", "update-1", tmp_path)
        recovery.reconcile_database_state(tmp_path, attempts=1)
        assert _database_state()["state"] == "NORMAL"
    finally:
        with psycopg.connect(database) as connection:
            connection.execute("DROP TABLE IF EXISTS system_operating_state")
