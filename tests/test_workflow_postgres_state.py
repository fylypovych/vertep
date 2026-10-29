"""Workflow state transactions against disposable PostgreSQL, without fake DB calls."""

import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from core.workflow.database_state import DatabaseWorkflowState
from tests.test_text_redis_process import port, wait


def assert_durable_state(url):
    state = DatabaseWorkflowState("postgres", url)
    job = str(uuid4())
    document = {"status": "PAUSED", "tasks": {"a": {"output": {"artifact": "persisted"}, "approval": "PENDING", "revision": 2}}}
    state.save_job(job, document)
    fresh = DatabaseWorkflowState("postgres", url)
    assert fresh.load_job(job)["tasks"] == document["tasks"]
    document["tasks"]["a"]["approval"] = "APPROVED"
    fresh.save_job(job, document)
    assert state.load_job(job)["tasks"]["a"]["approval"] == "APPROVED"
    assert job in fresh.list_checkpoints()


def test_postgres_workflow_output_and_approval_survive_reconnect(tmp_path):
    external_url = os.getenv("VERTEP_TEST_DATABASE_URL")
    if external_url:
        assert_durable_state(external_url)
        return
    pg_bin = Path(os.getenv("VERTEP_TEST_POSTGRES_BIN", "/usr/lib/postgresql/16/bin"))
    suffix = ".exe" if os.name == "nt" else ""
    if not (pg_bin / ("initdb" + suffix)).is_file():
        if os.getenv("VERTEP_REQUIRE_WORKFLOW_POSTGRES") == "1":
            pytest.fail("Actual PostgreSQL is required for workflow acceptance")
        pytest.skip("Disposable PostgreSQL binaries are unavailable")
    data = tmp_path / "postgres"
    initialized = subprocess.run([str(pg_bin / ("initdb" + suffix)), "-D", str(data), "-U", "vertep", "-A", "trust", "--encoding=UTF8", "--no-locale"], capture_output=True, text=True)
    assert initialized.returncode == 0, initialized.stderr
    pg_port = port()
    log = (tmp_path / "postgres.log").open("w", encoding="utf-8")
    process = subprocess.Popen([str(pg_bin / ("postgres" + suffix)), "-D", str(data), "-h", "127.0.0.1", "-p", str(pg_port)], stdout=log, stderr=log)
    url = f"postgresql://vertep@127.0.0.1:{pg_port}/postgres"
    try:
        import psycopg
        def ready():
            assert process.poll() is None
            try:
                with psycopg.connect(url, connect_timeout=1) as connection:
                    return connection.execute("SELECT 1").fetchone() == (1,)
            except psycopg.Error:
                return False
        wait(ready)
        assert_durable_state(url)
    finally:
        subprocess.run([str(pg_bin / ("pg_ctl" + suffix)), "-D", str(data), "-m", "fast", "stop"], capture_output=True, timeout=20)
        process.wait(timeout=20)
        log.close()
