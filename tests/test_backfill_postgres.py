"""Issue #135: PostgreSQL backfill integration against a disposable database.

These tests KEEP the existing fake-connection unit tests in tests/test_backfill.py
and add the missing real-database evidence. They run against a disposable
PostgreSQL described by VERTEP_TEST_DATABASE_URL (falling back to DATABASE_URL).
When neither is configured the tests skip, which does NOT count as a PASS — CI
provisions a PostgreSQL service so the required integration set actually runs.
"""

import importlib.util
import os
import uuid
from pathlib import Path

import pytest

from scripts.migrate import run_backfills

DATABASE_URL = os.getenv("VERTEP_TEST_DATABASE_URL") or os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="VERTEP_TEST_DATABASE_URL/DATABASE_URL not set; no disposable PostgreSQL available",
)


def _connect():
    psycopg = pytest.importorskip("psycopg")
    return psycopg.connect(DATABASE_URL)


@pytest.fixture
def backfill_root(tmp_path):
    return tmp_path / "db"


@pytest.fixture
def connection():
    with _connect() as conn:
        yield conn


def _write(root: Path, name: str, code: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(code, encoding="utf-8")


def test_batches_checkpoint_restart_and_no_duplicates(connection, backfill_root):
    """Resume from a persisted checkpoint and never duplicate already-applied rows."""
    suffix = uuid.uuid4().hex[:8]
    table = f"bf_resume_{suffix}"
    script = f"{900 + (int(suffix, 16) % 100)}_resume_{suffix}.backfill.py"
    connection.execute(f"CREATE TABLE {table}(value INTEGER)")
    connection.commit()
    _write(backfill_root, script, (
        "def run_batch(connection, checkpoint):\n"
        f"    value = int(checkpoint.get('value', 0)) + 1\n"
        f"    connection.execute('INSERT INTO {table}(value) VALUES(%s)', (value,))\n"
        "    if value >= 3:\n"
        "        return {'done': True, 'checkpoint': {'value': value, 'done': True}}\n"
        "    return {'done': False, 'checkpoint': {'value': value}}\n"
    ))
    # Simulate an interruption after the first batch was durably checkpointed.
    connection.execute("""CREATE TABLE IF NOT EXISTS migration_backfills(
        name TEXT PRIMARY KEY, checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
        batches BIGINT NOT NULL DEFAULT 0, completed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    connection.execute(
        "INSERT INTO migration_backfills(name,checkpoint,batches) VALUES(%s,%s::jsonb,1)"
        " ON CONFLICT(name) DO NOTHING", (script, '{"value": 1}'))
    connection.commit()

    completed = run_backfills(backfill_root, connection)
    assert script in completed
    rows = [row[0] for row in connection.execute(f"SELECT value FROM {table} ORDER BY value")]
    # Resumed from checkpoint=1: only 2 and 3 are produced, 1 is NOT duplicated.
    assert rows == [2, 3]

    # A repeat run is idempotent: completed_at is set, so nothing is re-applied.
    run_backfills(backfill_root, connection)
    rows_again = [row[0] for row in connection.execute(f"SELECT value FROM {table} ORDER BY value")]
    assert rows_again == [2, 3]
    batches = connection.execute(
        "SELECT batches FROM migration_backfills WHERE name=%s", (script,)).fetchone()[0]
    assert batches == 3

    connection.execute(f"DROP TABLE {table}")
    connection.execute("DELETE FROM migration_backfills WHERE name=%s", (script,))
    connection.commit()


def test_backfill_preserves_existing_and_deleted_records(connection, backfill_root):
    """A backfill must not delete pre-existing rows nor resurrect deleted ones."""
    suffix = uuid.uuid4().hex[:8]
    table = f"bf_jobs_{suffix}"
    script = f"{900 + (int(suffix, 16) % 100)}_jobs_{suffix}.backfill.py"
    connection.execute(f"CREATE TABLE {table}(job_id TEXT PRIMARY KEY, state TEXT)")
    connection.execute(f"INSERT INTO {table}(job_id,state) VALUES('2024-000001','kept')")
    connection.execute(f"INSERT INTO {table}(job_id,state) VALUES('2024-000002','deleted')")
    connection.commit()
    _write(backfill_root, script, (
        "def run_batch(connection, checkpoint):\n"
        "    if checkpoint.get('done'):\n"
        "        return {'done': True, 'checkpoint': checkpoint}\n"
        f"    connection.execute(\"INSERT INTO {table}(job_id,state) VALUES('2024-000003','new')\"\n"
        "                       ' ON CONFLICT(job_id) DO NOTHING')\n"
        "    return {'done': True, 'checkpoint': {'done': True}}\n"
    ))

    run_backfills(backfill_root, connection)
    rows = dict(connection.execute(f"SELECT job_id,state FROM {table}"))
    assert rows == {"2024-000001": "kept", "2024-000002": "deleted", "2024-000003": "new"}

    # Repeating backfill (allowing the backfill to run again) still does not duplicate.
    connection.execute("DELETE FROM migration_backfills WHERE name=%s", (script,))
    connection.commit()
    run_backfills(backfill_root, connection)
    assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 3

    connection.execute(f"DROP TABLE {table}")
    connection.execute("DELETE FROM migration_backfills WHERE name=%s", (script,))
    connection.commit()
