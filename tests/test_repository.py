import json
import os
import tempfile
from pathlib import Path

import pytest

from core.repository import FileRepository, MemoryRepository, StateRepository
from core.models import Channel, Job, JobStatus


class TestFileRepositoryChannels:
    def test_list_channels_empty(self, tmp_path):
        repo = FileRepository(tmp_path)
        assert repo.list_channels() == []

    def test_list_channels_with_brand_filter(self, tmp_path):
        repo = FileRepository(tmp_path)
        repo.save_channel(Channel(channel_id="ch-1", brand_id="b1", channel_type="telegram", target="@t1"))
        repo.save_channel(Channel(channel_id="ch-2", brand_id="b2", channel_type="telegram", target="@t2"))
        assert len(repo.list_channels("b1")) == 1
        assert repo.list_channels("b1")[0].channel_id == "ch-1"

    def test_get_channel_not_found(self, tmp_path):
        repo = FileRepository(tmp_path)
        assert repo.get_channel("nonexistent") is None

    def test_save_and_get_channel(self, tmp_path):
        repo = FileRepository(tmp_path)
        ch = Channel(channel_id="ch-1", brand_id="b1", channel_type="youtube", target="UC123")
        repo.save_channel(ch)
        fetched = repo.get_channel("ch-1")
        assert fetched is not None
        assert fetched.channel_id == "ch-1"
        assert fetched.channel_type == "youtube"
        assert fetched.target == "UC123"

    def test_update_channel(self, tmp_path):
        repo = FileRepository(tmp_path)
        ch = Channel(channel_id="ch-1", brand_id="b1", channel_type="telegram", target="@old")
        repo.save_channel(ch)
        ch.target = "@new"
        ch.enabled = False
        repo.save_channel(ch)
        fetched = repo.get_channel("ch-1")
        assert fetched.target == "@new"
        assert fetched.enabled is False

    def test_delete_channel(self, tmp_path):
        repo = FileRepository(tmp_path)
        repo.save_channel(Channel(channel_id="ch-1", brand_id="b1", channel_type="telegram", target="@t"))
        repo.delete_channel("ch-1")
        assert repo.get_channel("ch-1") is None
        assert repo.list_channels() == []

    def test_persistence_across_instances(self, tmp_path):
        repo1 = FileRepository(tmp_path)
        repo1.save_channel(Channel(channel_id="ch-1", brand_id="b1", channel_type="telegram", target="@t1"))
        repo1.save_channel(Channel(channel_id="ch-2", brand_id="b2", channel_type="youtube", target="UC2"))

        repo2 = FileRepository(tmp_path)
        assert len(repo2.list_channels()) == 2
        assert repo2.get_channel("ch-1").target == "@t1"
        assert repo2.get_channel("ch-2").channel_type == "youtube"

    def test_channel_file_is_atomic(self, tmp_path):
        repo = FileRepository(tmp_path)
        ch = Channel(channel_id="ch-1", brand_id="b1", channel_type="telegram", target="@t")
        repo.save_channel(ch)
        channel_file = tmp_path / ".channels.json"
        assert channel_file.exists()
        # No .tmp file should remain
        assert not (tmp_path / ".channels.json.tmp").exists()


class TestFileRepositoryJobs:
    def test_save_and_load_job(self, tmp_path):
        repo = FileRepository(tmp_path)
        job = Job(
            job_id="2024-000001",
            topic="Test topic",
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
        assert loaded[0].topic == "Test topic"

    def test_job_file_structure(self, tmp_path):
        repo = FileRepository(tmp_path)
        job = Job(
            job_id="2024-000001",
            topic="Test",
            character_id="did_test",
            priority=5,
            status=JobStatus.NEW,
            created_at="2024-01-01T00:00:00Z",
        )
        repo.save_job(job)
        job_dir = tmp_path / "2024-000001"
        assert job_dir.is_dir()
        assert (job_dir / "job.json").is_file()

    def test_append_event(self, tmp_path):
        repo = FileRepository(tmp_path)
        repo.append_event("2024-000001", "2024-01-01T01:00:00Z", "EVENT 1")
        repo.append_event("2024-000001", "2024-01-01T02:00:00Z", "EVENT 2")
        events_file = tmp_path / "2024-000001" / "events.jsonl"
        assert events_file.exists()
        lines = events_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert "EVENT 1" in lines[0]
        assert "EVENT 2" in lines[1]

    def test_record_task(self, tmp_path):
        repo = FileRepository(tmp_path)
        repo.record_task({"task_id": "task-1", "job_id": "job-1"}, "COMPLETED", "worker-1")
        tasks_file = tmp_path / ".tasks.jsonl"
        assert tasks_file.exists()

    def test_telegram_updates(self, tmp_path):
        repo = FileRepository(tmp_path)
        assert repo.has_telegram_update("chat1", "msg1") is False
        repo.record_telegram_update("chat1", "msg1", {"data": "test"})
        assert repo.has_telegram_update("chat1", "msg1") is True


class TestMemoryRepository:
    def test_channel_crud(self):
        repo = MemoryRepository()
        ch = Channel(channel_id="ch-1", brand_id="b1", channel_type="telegram", target="@t")
        repo.save_channel(ch)
        assert repo.get_channel("ch-1") == ch
        assert len(repo.list_channels()) == 1
        repo.delete_channel("ch-1")
        assert repo.get_channel("ch-1") is None

    def test_job_crud(self):
        repo = MemoryRepository()
        job = Job(
            job_id="job-1", topic="Test", character_id="did_test", priority=5,
            status=JobStatus.NEW, created_at="2024-01-01T00:00:00Z",
        )
        repo.save_job(job)
        loaded = list(repo.load_jobs())
        assert len(loaded) == 1
        repo.delete_job("job-1")
        assert list(repo.load_jobs()) == []

    def test_worker_crud(self):
        repo = MemoryRepository()
        worker = {"node_name": "w1", "status": "READY", "capabilities": ["image_generation"]}
        repo.save_worker(worker)
        workers = list(repo.load_workers())
        assert len(workers) == 1
        assert workers[0]["node_name"] == "w1"

    def test_events_and_tasks(self):
        repo = MemoryRepository()
        repo.append_event("job-1", "2024-01-01T00:00:00Z", "TEST")
        repo.record_task({"task_id": "t1", "job_id": "job-1"}, "COMPLETED")
        assert len(repo.events) == 1
        assert "t1" in repo.tasks


class TestRepositoryInterface:
    def test_state_repository_abstract_methods(self):
        """Verify StateRepository defines all required abstract methods."""
        import inspect
        methods = [
            "next_job_sequence", "load_jobs", "save_job", "delete_job",
            "save_worker", "load_workers", "append_event", "record_task",
            "has_telegram_update", "record_telegram_update",
            "list_channels", "get_channel", "save_channel", "delete_channel",
        ]
        for method in methods:
            assert hasattr(StateRepository, method)
            attr = getattr(StateRepository, method)
            # Check if it's an abstractmethod by inspecting the function
            assert inspect.isfunction(attr) or hasattr(attr, '__isabstractmethod__')


class TestFileRepositoryWorkers:
    def test_save_and_load_workers(self, tmp_path):
        repo = FileRepository(tmp_path)
        w1 = {"node_name": "w1", "role": "gpu", "status": "READY", "capabilities": ["image_generation"]}
        w2 = {"node_name": "w2", "role": "text", "status": "BUSY", "capabilities": ["text_generation"]}
        repo.save_worker(w1)
        repo.save_worker(w2)
        workers = list(repo.load_workers())
        assert len(workers) == 2

    def test_load_workers_with_filters(self, tmp_path):
        repo = FileRepository(tmp_path)
        repo.save_worker({"node_name": "w1", "role": "gpu", "status": "READY", "capabilities": ["image_generation"]})
        repo.save_worker({"node_name": "w2", "role": "gpu", "status": "BUSY", "capabilities": ["image_generation"]})
        repo.save_worker({"node_name": "w3", "role": "text", "status": "READY", "capabilities": ["text_generation"]})

        gpu_workers = list(repo.load_workers(role="gpu"))
        assert len(gpu_workers) == 2

        ready_workers = list(repo.load_workers(status="READY"))
        assert len(ready_workers) == 2

        text_capable = list(repo.load_workers(capability="text_generation"))
        assert len(text_capable) == 1


class TestJobSequence:
    def test_file_repository_next_sequence(self, tmp_path):
        repo = FileRepository(tmp_path)
        assert repo.next_job_sequence(2024, 0) == 1
        assert repo.next_job_sequence(2024, 5) == 6

    def test_memory_repository_next_sequence(self):
        repo = MemoryRepository()
        assert repo.next_job_sequence(2024, 10) == 11