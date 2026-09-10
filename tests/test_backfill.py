import json
import os
import tempfile
from contextlib import nullcontext
from pathlib import Path

import pytest

from core.repository import FileRepository, MemoryRepository, build_repository
from core.models import Channel, Job, JobStatus
from scripts.migrate import run_backfills


class Cursor:
    def __init__(self, row=None):
        self.row = row
        self.rows = [row] if row else []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class Connection:
    def __init__(self):
        self.batches = 0
        self.data = {}
        self.tables = {}

    def execute(self, query, params=()):
        if "SELECT checkpoint" in query:
            name = params[0] if params else ""
            cp = self.data.get(name, {"checkpoint": {}, "completed_at": None})
            # Return a tuple (checkpoint, completed_at) to match the SQL query
            return Cursor((cp.get("checkpoint", {}), cp.get("completed_at")))
        if "INSERT INTO migration_backfills" in query:
            self.batches += 1
            name = params[0]
            checkpoint = json.loads(params[1]) if params[1] else {}
            done = params[2]
            self.data[name] = {"checkpoint": checkpoint, "completed_at": "now" if done else None}
        if "CREATE TABLE" in query:
            return Cursor()
        return Cursor()

    def commit(self):
        return None

    def transaction(self):
        return nullcontext()


def test_backfill_is_batched_and_checkpointed(tmp_path):
    (tmp_path / "009_items.backfill.py").write_text(
        "def run_batch(connection, checkpoint):\n"
        "    value = int(checkpoint.get('value', 0)) + 1\n"
        "    return {'done': value == 2, 'checkpoint': {'value': value}}\n",
        encoding="utf-8")
    connection = Connection()
    assert run_backfills(tmp_path, connection) == ["009_items.backfill.py"]
    assert connection.batches == 2


def test_file_repository_channel_crud(tmp_path):
    repo = FileRepository(tmp_path)

    ch1 = Channel(channel_id="ch-001", brand_id="brand01", channel_type="telegram", target="@test")
    ch2 = Channel(channel_id="ch-002", brand_id="brand01", channel_type="youtube", target="UC123")
    ch3 = Channel(channel_id="ch-003", brand_id="brand02", channel_type="telegram", target="@other")

    repo.save_channel(ch1)
    repo.save_channel(ch2)
    repo.save_channel(ch3)

    all_channels = repo.list_channels()
    assert len(all_channels) == 3

    brand_channels = repo.list_channels("brand01")
    assert len(brand_channels) == 2
    assert {c.channel_id for c in brand_channels} == {"ch-001", "ch-002"}

    fetched = repo.get_channel("ch-001")
    assert fetched is not None
    assert fetched.channel_type == "telegram"
    assert fetched.target == "@test"

    repo.delete_channel("ch-001")
    assert repo.get_channel("ch-001") is None
    assert len(repo.list_channels()) == 2

    # Persistence across new instance
    repo2 = FileRepository(tmp_path)
    assert len(repo2.list_channels()) == 2
    assert repo2.get_channel("ch-002").target == "UC123"


def test_file_repository_job_persistence(tmp_path):
    repo = FileRepository(tmp_path)

    job = Job(
        job_id="2024-000001",
        topic="Test job",
        character_id="did_test",
        priority=5,
        status=JobStatus.NEW,
        created_at="2024-01-01T00:00:00Z",
        source="web",
    )
    repo.save_job(job)

    loaded = list(repo.load_jobs())
    assert len(loaded) == 1
    assert loaded[0].job_id == "2024-000001"
    assert loaded[0].topic == "Test job"

    # Verify file exists
    job_file = tmp_path / "2024-000001" / "job.json"
    assert job_file.exists()

    # Verify events file can be appended
    repo.append_event("2024-000001", "2024-01-01T01:00:00Z", "TEST EVENT")
    events_file = tmp_path / "2024-000001" / "events.jsonl"
    assert events_file.exists()


def test_memory_repository_channel_crud():
    repo = MemoryRepository()

    ch1 = Channel(channel_id="ch-001", brand_id="brand01", channel_type="telegram", target="@test")
    ch2 = Channel(channel_id="ch-002", brand_id="brand02", channel_type="youtube", target="UC123")

    repo.save_channel(ch1)
    repo.save_channel(ch2)

    assert len(repo.list_channels()) == 2
    assert len(repo.list_channels("brand01")) == 1
    assert repo.get_channel("ch-001").target == "@test"

    repo.delete_channel("ch-001")
    assert repo.get_channel("ch-001") is None
    assert len(repo.list_channels()) == 1


def test_build_repository_file_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "file")
    monkeypatch.setenv("JOB_ROOT", str(tmp_path))
    repo = build_repository(tmp_path)
    assert isinstance(repo, FileRepository)


def test_build_repository_memory_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "memory")
    repo = build_repository("jobs")
    assert isinstance(repo, MemoryRepository)


def test_backfill_phases_complete(monkeypatch, tmp_path):
    """Test that the backfill completes all phases without error."""
    # Create a mock connection that tracks all phase transitions
    class BackfillConnection:
        def __init__(self):
            self.batches = 0
            self.checkpoints = {}
            self.phase_data = {}

        def execute(self, query, params=()):
            if "SELECT checkpoint" in query:
                name = params[0] if params else ""
                cp = self.checkpoints.get(name, {"checkpoint": {}, "completed_at": None})
                return Cursor((cp.get("checkpoint", {}), cp.get("completed_at")))
            if "INSERT INTO migration_backfills" in query:
                self.batches += 1
                name = params[0]
                checkpoint = json.loads(params[1]) if params[1] else {}
                done = params[2]
                self.checkpoints[name] = {"checkpoint": checkpoint, "completed_at": "now" if done else None}
            if "CREATE TABLE" in query:
                return Cursor()
            return Cursor()

        def commit(self):
            return None

        def transaction(self):
            return nullcontext()

    # Create test backfill that simulates all phases
    backfill_code = '''
def run_batch(connection, checkpoint):
    phase = checkpoint.get("phase", "jobs")
    offset = checkpoint.get("offset", 0)

    if phase == "jobs":
        return {"done": False, "checkpoint": {"phase": "events", "offset": 0}}
    if phase == "events":
        return {"done": False, "checkpoint": {"phase": "tasks", "offset": 0}}
    if phase == "tasks":
        return {"done": False, "checkpoint": {"phase": "scenes", "offset": 0}}
    if phase == "scenes":
        return {"done": False, "checkpoint": {"phase": "artifacts", "offset": 0}}
    if phase == "artifacts":
        return {"done": False, "checkpoint": {"phase": "stage_attempts", "offset": 0}}
    if phase == "stage_attempts":
        return {"done": False, "checkpoint": {"phase": "publications", "offset": 0}}
    if phase == "publications":
        return {"done": False, "checkpoint": {"phase": "telegram", "offset": 0}}
    if phase == "telegram":
        return {"done": False, "checkpoint": {"phase": "workers", "offset": 0}}
    if phase == "workers":
        return {"done": False, "checkpoint": {"phase": "channels", "offset": 0}}
    if phase == "channels":
        return {"done": True, "checkpoint": {"done": True}}
    return {"done": True, "checkpoint": {"done": True}}
'''
    (tmp_path / "010_test.backfill.py").write_text(backfill_code, encoding="utf-8")

    conn = BackfillConnection()
    result = run_backfills(tmp_path, conn)
    assert "010_test.backfill.py" in result
    # Should have run through all phases: jobs, events, tasks, scenes, artifacts, stage_attempts, publications, telegram, workers, channels
    assert conn.batches >= 10


def test_backfill_resumes_from_checkpoint(tmp_path):
    """Test that backfill resumes from the last checkpoint."""
    backfill_code = '''
def run_batch(connection, checkpoint):
    value = int(checkpoint.get("value", 0)) + 1
    if value >= 5:
        return {"done": True, "checkpoint": {"value": value, "done": True}}
    return {"done": False, "checkpoint": {"value": value}}
'''
    (tmp_path / "011_resume.backfill.py").write_text(backfill_code, encoding="utf-8")

    # First run - simulate checkpoint at value=2
    class MockConn:
        def __init__(self, initial_cp=None):
            self.batches = 0
            self.cp = initial_cp or {"value": 2}

        def execute(self, query, params=()):
            if "SELECT checkpoint" in query:
                return Cursor((self.cp, None))
            if "INSERT INTO migration_backfills" in query:
                self.batches += 1
                self.cp = json.loads(params[1]) if params[1] else {}
            return Cursor()

        def commit(self): return None
        def transaction(self): return nullcontext()

    conn = MockConn()
    result = run_backfills(tmp_path, conn)
    assert "011_resume.backfill.py" in result
    # Should need 3 more batches (3, 4, 5)
    assert conn.batches == 3