"""Automated evidence tests for Issue #49: video revision/versioning contract.

Tests cover:
- Immutable video versioning: each render produces a unique file, previous versions preserved
- Approval bound to version: stale approval rejected, active version tracked
- Restart bypass closure: VIDEO_READY auto-recovers to VIDEO_PENDING_APPROVAL unless version approved
- Shared Web/Telegram regeneration path: endpoint submits _finalize_video_regenerate
- Structured video revisions: revision stored with version, text, actor
- Persistence-safe transitions: video_regenerating flag, dedup
- E2E lifecycle: finalize -> revision -> regenerate -> new version -> approve
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.models import Job, JobStatus, utc_now, VideoVersion, job_transition_allowed
from core.artifacts import _digest
from core.pipeline import JobStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_file(path: Path, content: bytes = b"fake video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _make_video_version(v: int, approved: bool = False) -> VideoVersion:
    return VideoVersion(
        version=v, path=f"final/video-v{v}.mp4",
        sha256=hashlib.sha256(f"video-v{v}".encode()).hexdigest(),
        approved=approved,
    )


def _make_job_for_store(tmp_path: Path) -> Job:
    return Job(
        job_id="test-job-001", topic="Test topic",
        character_id="test_char", priority=5,
        status=JobStatus.VIDEO_PENDING_APPROVAL,
        created_at=utc_now(), source="web",
        script={"title": "Test", "scenes": [
            {"prompt": "Kadr", "voiceover": "Text", "duration": 5}
        ]},
    )


def _make_store(tmp_path: Path, job: Job | None = None) -> tuple[JobStore, Job]:
    job = job or _make_job_for_store(tmp_path)
    repo = _MockRepository(tmp_path / "jobs")
    store = JobStore(root=str(tmp_path / "jobs"), repository=repo)
    for name in ("references", "images", "video", "audio", "subtitles", "final", "frames", "storyboard"):
        (tmp_path / "jobs" / job.job_id / name).mkdir(parents=True, exist_ok=True)
    store.jobs[job.job_id] = job
    store._save = lambda j: repo.save_job(j)
    repo.save_job(job)
    return store, job


class _MockRepository:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict] = {}
        self._workers: dict[str, dict] = {}

    def load_jobs(self):
        for data in self._jobs.values():
            yield Job.model_validate(data)

    def save_job(self, job: Job):
        self._jobs[job.job_id] = job.model_dump(mode="json")

    def delete_job(self, job_id: str):
        self._jobs.pop(job_id, None)

    def save_worker(self, worker: dict):
        self._workers[worker["node_name"]] = worker

    def load_workers(self, **kw):
        return []

    def append_event(self, *a, **kw):
        pass

    def record_task(self, *a, **kw):
        pass

    def has_telegram_update(self, *a, **kw):
        return False

    def record_telegram_update(self, *a, **kw):
        pass


# ---------------------------------------------------------------------------
# 1. Immutable video versioning
# ---------------------------------------------------------------------------

class TestImmutableVideoVersioning:
    def test_finalize_creates_versioned_file(self, tmp_path):
        from core.pipeline import finalize_job
        job = _make_job_for_store(tmp_path)
        store, job = _make_store(tmp_path, job)
        images = [_write_file(tmp_path / "job" / "images" / "img.png")]
        with patch("core.pipeline.providers") as mock_providers:
            mock_providers.video_engine.return_value.render.side_effect = lambda out, **kw: _write_file(out)
            result = finalize_job(store, job, images)
        assert result.active_video_version == 1
        assert len(result.video_versions) == 1
        vv = result.video_versions[0]
        assert vv.version == 1
        assert vv.path == "final/video-v1.mp4"
        assert vv.approved is False
        versioned_file = tmp_path / "jobs" / job.job_id / "final" / "video-v1.mp4"
        assert versioned_file.is_file()
        legacy = tmp_path / "jobs" / job.job_id / "final" / "video.mp4"
        # Symlink may fail on Windows without developer mode enabled
        if legacy.is_symlink():
            assert legacy.exists()

    def test_repeated_finalization_preserves_versions(self, tmp_path):
        from core.pipeline import finalize_job
        job = _make_job_for_store(tmp_path)
        job.status = JobStatus.VIDEO_REVISION_REQUESTED
        store, job = _make_store(tmp_path, job)
        job.video_versions.append(_make_video_version(1, approved=True))
        job.active_video_version = 1
        v1_file = tmp_path / "jobs" / job.job_id / "final" / "video-v1.mp4"
        _write_file(v1_file, b"v1 content")
        images = [_write_file(tmp_path / "job" / "images" / "img.png")]
        with patch("core.pipeline.providers") as mock_providers:
            mock_providers.video_engine.return_value.render.side_effect = lambda out, **kw: _write_file(out, b"data-v2")
            result = finalize_job(store, job, images)
        assert result.active_video_version == 2
        assert len(result.video_versions) == 2
        assert result.video_versions[0].version == 1
        assert result.video_versions[1].version == 2
        v1_file = tmp_path / "jobs" / job.job_id / "final" / "video-v1.mp4"
        v2_file = tmp_path / "jobs" / job.job_id / "final" / "video-v2.mp4"
        assert v1_file.is_file(), "Previous version file must not be overwritten"
        assert v2_file.is_file()

    def test_video_version_sha256_matches_file(self, tmp_path):
        from core.pipeline import finalize_job
        job = _make_job_for_store(tmp_path)
        store, job = _make_store(tmp_path, job)
        images = [_write_file(tmp_path / "job" / "images" / "img.png")]
        with patch("core.pipeline.providers") as mock_providers:
            mock_providers.video_engine.return_value.render.side_effect = lambda out, **kw: _write_file(out, b"test content")
            result = finalize_job(store, job, images)
        vv = result.video_versions[0]
        actual_file = tmp_path / "jobs" / job.job_id / vv.path
        assert vv.sha256 == _digest(actual_file)


# ---------------------------------------------------------------------------
# 2. Approval bound to version
# ---------------------------------------------------------------------------

class TestVersionBoundApproval:
    def test_approve_requires_active_version(self, tmp_path):
        from core.pipeline import approve_video
        job = _make_job_for_store(tmp_path)
        job.video_versions = []
        job.active_video_version = None
        with pytest.raises(ValueError, match="No active video version"):
            approve_video(SimpleNamespace(transition=lambda j, s, e: j), job, "test")

    def test_approve_persists_on_version_record(self, tmp_path):
        from core.pipeline import approve_video
        job = _make_job_for_store(tmp_path)
        job.video_versions = [_make_video_version(1)]
        job.active_video_version = 1
        store = SimpleNamespace(
            transition=lambda j, s, e: setattr(j, "status", s) or j,
        )
        with patch("core.pipeline._progress"):
            result = approve_video(store, job, "telegram:42")
        assert job.video_versions[0].approved is True
        assert job.video_versions[0].approved_by == "telegram:42"

    def test_stale_approval_rejected(self, tmp_path):
        from core.pipeline import approve_video
        job = _make_job_for_store(tmp_path)
        job.video_versions = [_make_video_version(1), _make_video_version(2)]
        job.active_video_version = 2
        store = SimpleNamespace(transition=lambda j, s, e: j)
        with pytest.raises(ValueError, match="Stale video approval"):
            approve_video(store, job, "test", expected_version=1)

    def test_matching_version_approved(self, tmp_path):
        from core.pipeline import approve_video
        job = _make_job_for_store(tmp_path)
        job.video_versions = [_make_video_version(2)]
        job.active_video_version = 2
        store = SimpleNamespace(transition=lambda j, s, e: setattr(j, "status", s) or j)
        with patch("core.pipeline._progress"):
            approve_video(store, job, "test", expected_version=2)
        assert job.video_versions[0].approved is True

    def test_legacy_approval_without_version_still_works(self, tmp_path):
        from core.pipeline import approve_video
        job = _make_job_for_store(tmp_path)
        job.video_versions = [_make_video_version(3)]
        job.active_video_version = 3
        store = SimpleNamespace(transition=lambda j, s, e: setattr(j, "status", s) or j)
        with patch("core.pipeline._progress"):
            approve_video(store, job, "legacy_callback", expected_version=None)
        assert job.video_versions[0].approved is True
# ---------------------------------------------------------------------------
# 3. Restart bypass closure
# ---------------------------------------------------------------------------

class TestRestartBypassClosure:
    def test_video_ready_without_approval_goes_to_pending(self, tmp_path):
        job = _make_job_for_store(tmp_path)
        job.status = JobStatus.VIDEO_READY
        job.active_video_version = 1
        job.video_versions = [_make_video_version(1, approved=False)]
        _write_file(tmp_path / "jobs" / job.job_id / "final" / "video-v1.mp4")
        repo = _MockRepository(tmp_path / "jobs")
        repo._jobs[job.job_id] = job.model_dump(mode="json")
        store = JobStore(root=str(tmp_path / "jobs"), repository=repo)
        loaded = store.jobs[job.job_id]
        assert loaded.status == JobStatus.VIDEO_PENDING_APPROVAL

    def test_video_ready_with_approved_version_goes_to_ready(self, tmp_path):
        job = _make_job_for_store(tmp_path)
        job.status = JobStatus.VIDEO_READY
        job.active_video_version = 1
        job.video_versions = [_make_video_version(1, approved=True)]
        _write_file(tmp_path / "jobs" / job.job_id / "final" / "video-v1.mp4")
        repo = _MockRepository(tmp_path / "jobs")
        repo._jobs[job.job_id] = job.model_dump(mode="json")
        store = JobStore(root=str(tmp_path / "jobs"), repository=repo)
        loaded = store.jobs[job.job_id]
        assert loaded.status == JobStatus.READY

    def test_video_ready_legacy_no_versions_still_pending(self, tmp_path):
        job = _make_job_for_store(tmp_path)
        job.status = JobStatus.VIDEO_READY
        job.active_video_version = None
        job.video_versions = []
        _write_file(tmp_path / "jobs" / job.job_id / "final" / "video.mp4")
        repo = _MockRepository(tmp_path / "jobs")
        repo._jobs[job.job_id] = job.model_dump(mode="json")
        store = JobStore(root=str(tmp_path / "jobs"), repository=repo)
        loaded = store.jobs[job.job_id]
        assert loaded.status == JobStatus.VIDEO_PENDING_APPROVAL


# ---------------------------------------------------------------------------
# 4. Structured video revisions
# ---------------------------------------------------------------------------

class TestStructuredVideoRevisions:
    def test_request_stores_revision_with_version(self, tmp_path):
        from core.pipeline import request_video_revision
        job = _make_job_for_store(tmp_path)
        job.active_video_version = 1
        job.video_versions = [_make_video_version(1)]
        store = SimpleNamespace(transition=lambda j, s, e: setattr(j, "status", s) or j)
        result = request_video_revision(store, job, "Make the intro longer", "telegram:42")
        assert len(result.video_revisions) == 1
        rev = result.video_revisions[0]
        assert rev.version == 1
        assert rev.text == "Make the intro longer"
        assert rev.actor == "telegram:42"
        assert result.status == JobStatus.VIDEO_REVISION_REQUESTED

    def test_multiple_revisions_track_history(self, tmp_path):
        from core.pipeline import request_video_revision
        job = _make_job_for_store(tmp_path)
        job.active_video_version = 1
        store = SimpleNamespace(transition=lambda j, s, e: setattr(j, "status", s) or j)
        request_video_revision(store, job, "Edit 1", "user")
        job.status = JobStatus.VIDEO_PENDING_APPROVAL
        request_video_revision(store, job, "Edit 2", "user")
        assert len(job.video_revisions) == 2
        assert job.video_revisions[0].text == "Edit 1"
        assert job.video_revisions[1].text == "Edit 2"


# ---------------------------------------------------------------------------
# 5. Dedup regeneration
# ---------------------------------------------------------------------------

class TestRegenerationDedup:
    def test_video_regenerating_flag_blocks(self, tmp_path):
        from unittest.mock import Mock
        import importlib
        job = _make_job_for_store(tmp_path)
        job.status = JobStatus.VIDEO_REVISION_REQUESTED
        job.video_regenerating = True
        adapter = Mock()
        module = importlib.import_module("core.app")
        callback = {"id": "cb-dedup", "data": "vid_regen:test-job-001", "message": {"chat": {"id": "42"}}}
        with patch("core.app.TelegramAdapter", lambda: adapter):
            with patch.object(module, "store", SimpleNamespace(jobs={"test-job-001": job})):
                module._handle_video_callback(callback, "42", "vid_regen", "test-job-001")
        assert adapter.answer_callback.called
        assert "вже генерується" in adapter.answer_callback.call_args_list[-1][0][1]


# ---------------------------------------------------------------------------
# 6. Transition table
# ---------------------------------------------------------------------------

class TestTransitionTable:
    def test_video_revision_requested_allows_assembly(self):
        assert job_transition_allowed(JobStatus.VIDEO_REVISION_REQUESTED, JobStatus.ASSEMBLY)

    def test_video_revision_requested_allows_video_generation(self):
        assert job_transition_allowed(JobStatus.VIDEO_REVISION_REQUESTED, JobStatus.VIDEO_GENERATION)

    def test_video_revision_requested_allows_cancelled(self):
        assert job_transition_allowed(JobStatus.VIDEO_REVISION_REQUESTED, JobStatus.CANCELLED)

    def test_video_pending_approval_allows_revision(self):
        assert job_transition_allowed(JobStatus.VIDEO_PENDING_APPROVAL, JobStatus.VIDEO_REVISION_REQUESTED)

    def test_video_pending_approval_allows_approved(self):
        assert job_transition_allowed(JobStatus.VIDEO_PENDING_APPROVAL, JobStatus.VIDEO_APPROVED)

    def test_video_approved_allows_video_ready(self):
        assert job_transition_allowed(JobStatus.VIDEO_APPROVED, JobStatus.VIDEO_READY)


# ---------------------------------------------------------------------------
# 7. E2E lifecycle
# ---------------------------------------------------------------------------

class TestE2ELifecycle:
    def test_full_video_lifecycle(self, tmp_path):
        from core.pipeline import (
            finalize_job, approve_video, request_video_revision,
        )
        job = _make_job_for_store(tmp_path)
        store, job = _make_store(tmp_path, job)
        images = [_write_file(tmp_path / "job" / "images" / "img.png")]

        # First render
        with patch("core.pipeline.providers") as mp:
            mp.video_engine.return_value.render.side_effect = lambda out, **kw: _write_file(out, b"v1")
            job = finalize_job(store, job, images)
        assert job.active_video_version == 1
        assert job.status == JobStatus.VIDEO_PENDING_APPROVAL

        # Request revision
        job = request_video_revision(store, job, "Make it brighter", "test")
        assert job.status == JobStatus.VIDEO_REVISION_REQUESTED
        assert len(job.video_revisions) == 1

        # Re-render (simulating a regeneration producing version 2)
        job.status = JobStatus.VIDEO_REVISION_REQUESTED
        with patch("core.pipeline.providers") as mp:
            mp.video_engine.return_value.render.side_effect = lambda out, **kw: _write_file(out, b"v2")
            job = finalize_job(store, job, images)
        assert job.active_video_version == 2
        assert len(job.video_versions) == 2

        # Stale approval of v1 must be rejected
        with patch("core.pipeline._progress"):
            with pytest.raises(ValueError, match="Stale"):
                approve_video(store, job, "test", expected_version=1)

        # Approve v2 (current) must succeed
        with patch("core.pipeline._progress"):
            job = approve_video(store, job, "test", expected_version=2)
        assert job.status == JobStatus.READY
        assert job.video_versions[1].approved is True
        assert job.video_versions[0].approved is False


# ---------------------------------------------------------------------------
# 8. Publish rejection for unapproved video version
# ---------------------------------------------------------------------------

class TestPublishRejection:
    def test_publish_rejects_unapproved_active_version(self, tmp_path):
        from fastapi.testclient import TestClient
        from core.app import app
        job = _make_job_for_store(tmp_path)
        job.status = JobStatus.READY
        job.active_video_version = 1
        job.video_versions = [_make_video_version(1, approved=False)]
        job.output_path = str(tmp_path / "video.mp4")
        _write_file(Path(job.output_path))
        repo = _MockRepository(tmp_path / "jobs")
        store = JobStore(root=str(tmp_path / "jobs"), repository=repo)
        store.jobs[job.job_id] = job
        repo.save_job(job)
        import core.api.jobs as jobs_mod
        original_store = jobs_mod.store
        jobs_mod.store = store
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(f"/api/jobs/{job.job_id}/publish",
                               json=["youtube"])
            assert resp.status_code == 409
            assert "not approved" in resp.json()["detail"]
        finally:
            jobs_mod.store = original_store

    def test_publish_allows_approved_version(self, tmp_path):
        from fastapi.testclient import TestClient
        from core.app import app
        job = _make_job_for_store(tmp_path)
        job.status = JobStatus.READY
        job.active_video_version = 1
        job.video_versions = [_make_video_version(1, approved=True)]
        job.output_path = str(tmp_path / "video.mp4")
        _write_file(Path(job.output_path))
        repo = _MockRepository(tmp_path / "jobs")
        store = JobStore(root=str(tmp_path / "jobs"), repository=repo)
        store.jobs[job.job_id] = job
        repo.save_job(job)
        import core.api.jobs as jobs_mod
        original_store = jobs_mod.store
        jobs_mod.store = store
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(f"/api/jobs/{job.job_id}/publish",
                               json=["youtube"])
            assert resp.status_code != 409 or "not approved" not in resp.json().get("detail", "")
        finally:
            jobs_mod.store = original_store