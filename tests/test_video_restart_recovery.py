"""Restart-recovery acceptance for Issue #49 video revision/versioning contract.

Uses the real disk-backed ``FileRepository`` persistence layer (no mock store)
to prove that video versions, approval evidence, and job state survive a CORE
restart, and that the restart-bypass approval gap is closed:

- An approved active video version restarts as READY (publishable).
- A ``VIDEO_READY`` job whose active version is NOT approved restarts as
  ``VIDEO_PENDING_APPROVAL`` and stays blocked from publication.
- A revision loop (render -> revision -> render v2 -> approve v2) persists all
  versions immutably; the previous version file/checksum is never overwritten,
  and the active/previous versions are individually tracked after restart.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from core.artifacts import _digest
from core.models import JobStatus
from core.pipeline import JobStore, finalize_job, approve_video, request_video_revision
from core.repository import FileRepository


def _write_file(path: Path, content: bytes = b"fake video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _build_store(tmp_path, topic: str = "Restart topic") -> tuple[JobStore, object]:
    root = tmp_path / "jobs"
    store = JobStore(root=str(root), repository=FileRepository(root))
    job = store.create(
        topic=topic, character_id="test_char", priority=5,
        source="web", task_type="image", brand_id="brand01",
    )
    job.script = {"title": topic, "scenes": [
        {"prompt": "Kadr", "voiceover": "Text", "duration": 5}]}
    return store, job


def _restart(root: Path) -> JobStore:
    """Simulate a CORE restart: a fresh store over the same persistent root."""
    return JobStore(root=str(root), repository=FileRepository(root))


def _image(store: JobStore, job: object) -> Path:
    return _write_file(store.root / job.job_id / "images" / "img.png")


class TestRestartRecovery:
    def test_approved_video_version_survives_restart(self, tmp_path):
        store, job = _build_store(tmp_path)
        image = _image(store, job)
        with patch("core.pipeline.providers") as mp:
            mp.video_engine.return_value.render.side_effect = (
                lambda out, **kw: _write_file(out))
            job = finalize_job(store, job, image)
        job = approve_video(store, job, "test", expected_version=1)
        assert job.status == JobStatus.READY
        assert job.video_versions[0].approved is True

        root = tmp_path / "jobs"
        loaded = _restart(root).jobs[job.job_id]

        assert loaded is not None
        assert loaded.job_id == job.job_id
        assert loaded.status == JobStatus.READY
        assert loaded.active_video_version == 1
        assert len(loaded.video_versions) == 1
        vv = loaded.video_versions[0]
        assert vv.approved is True
        assert vv.approved_by == "test"
        assert vv.approved_at is not None
        # checksum recorded matches the persisted artifact file
        assert vv.sha256 == _digest(root / job.job_id / "final" / "video-v1.mp4")

    def test_video_ready_interruption_restarts_to_pending_and_publish_blocked(self, tmp_path):
        """Simulate the interruption gap: finalize stored VIDEO_READY before
        VIDEO_PENDING_APPROVAL; the reload must NOT silently promote to READY."""
        store, job = _build_store(tmp_path)
        image = _image(store, job)
        with patch("core.pipeline.providers") as mp:
            mp.video_engine.return_value.render.side_effect = (
                lambda out, **kw: _write_file(out))
            job = finalize_job(store, job, image)
        assert job.status == JobStatus.VIDEO_PENDING_APPROVAL
        with store.lock:
            job.status = JobStatus.VIDEO_READY  # interruption between the two writes
            store._save(job)

        root = tmp_path / "jobs"
        restarted = _restart(root)
        loaded = restarted.jobs[job.job_id]
        assert loaded.status == JobStatus.VIDEO_PENDING_APPROVAL
        assert loaded.active_video_version == 1
        assert loaded.video_versions[0].approved is False

        # publication of the unapproved (restarted) job is rejected
        from fastapi.testclient import TestClient
        from core.app import app
        import core.api.jobs as jobs_mod
        original_store = jobs_mod.store
        jobs_mod.store = restarted
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"])
            assert resp.status_code == 409
            assert "approval" in resp.json()["detail"].lower()
        finally:
            jobs_mod.store = original_store

    def test_unapproved_fresh_restart_is_pending_not_publishable(self, tmp_path):
        store, job = _build_store(tmp_path)
        image = _image(store, job)
        with patch("core.pipeline.providers") as mp:
            mp.video_engine.return_value.render.side_effect = (
                lambda out, **kw: _write_file(out))
            job = finalize_job(store, job, image)

        root = tmp_path / "jobs"
        restarted = _restart(root)
        loaded = restarted.jobs[job.job_id]
        assert loaded.status == JobStatus.VIDEO_PENDING_APPROVAL
        assert loaded.active_video_version == 1

        from fastapi.testclient import TestClient
        from core.app import app
        import core.api.jobs as jobs_mod
        original_store = jobs_mod.store
        jobs_mod.store = restarted
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"])
            assert resp.status_code == 409
        finally:
            jobs_mod.store = original_store

    def test_revision_loop_versions_preserved_across_restart(self, tmp_path):
        store, job = _build_store(tmp_path)
        image = _image(store, job)
        with patch("core.pipeline.providers") as mp:
            mp.video_engine.return_value.render.side_effect = (
                lambda out, **kw: _write_file(out))
            job = finalize_job(store, job, image)  # v1, unapproved
        job = request_video_revision(store, job, "Make it brighter", "test")
        assert job.status == JobStatus.VIDEO_REVISION_REQUESTED
        with patch("core.pipeline.providers") as mp:
            mp.video_engine.return_value.render.side_effect = (
                lambda out, **kw: _write_file(out))
            job = finalize_job(store, job, image)  # v2, unapproved
        assert job.active_video_version == 2
        assert len(job.video_versions) == 2
        v1_sha = job.video_versions[0].sha256

        root = tmp_path / "jobs"
        loaded = _restart(root).jobs[job.job_id]
        assert loaded.active_video_version == 2
        assert loaded.status == JobStatus.VIDEO_PENDING_APPROVAL
        assert len(loaded.video_versions) == 2
        # previous version immutable: record + file + checksum preserved
        assert loaded.video_versions[0].version == 1
        assert loaded.video_versions[0].sha256 == v1_sha
        assert loaded.video_versions[1].version == 2
        assert (root / job.job_id / "final" / "video-v1.mp4").is_file()
        assert (root / job.job_id / "final" / "video-v2.mp4").is_file()

        # approve the active v2 after restart
        loaded = approve_video(_restart(root), loaded, "test", expected_version=2)
        assert loaded.status == JobStatus.READY
        assert loaded.video_versions[0].approved is False
        assert loaded.video_versions[1].approved is True

        # restart again: approved active version survives as READY
        reloaded = _restart(root).jobs[job.job_id]
        assert reloaded.status == JobStatus.READY
        assert reloaded.active_video_version == 2
        assert reloaded.video_versions[1].approved is True
        assert reloaded.video_versions[0].approved is False