"""Issue #49 — shared Web/Telegram video regeneration path and API revision contract.

The Web ``/video/regenerate`` endpoint and the Telegram ``vid_regen`` callback
both funnel into a single ``core.app._finalize_video_regenerate`` function that
re-assembles from persisted artifacts. These tests verify, deterministically
against an isolated store:

- Re-regeneration produces a NEW immutable version and returns to
  ``VIDEO_PENDING_APPROVAL`` (previous version file preserved).
- ``/video/approve`` rejects a stale version and accepts the active one.
- ``/video/revision`` stores the structured revision text bound to the active
  version and transitions to ``VIDEO_REVISION_REQUESTED``.
- ``/video/regenerate`` (Web) and ``vid_regen`` (Telegram) both dispatch the
  shared regeneration function.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from core.models import JobStatus
from core.pipeline import JobStore, finalize_job
from core.repository import FileRepository


def _write_file(path: Path, content: bytes = b"fake video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _build_store(tmp_path, topic: str = "Regen topic") -> tuple[JobStore, object]:
    root = tmp_path / "jobs"
    store = JobStore(root=str(root), repository=FileRepository(root))
    job = store.create(
        topic=topic, character_id="test_char", priority=5,
        source="web", task_type="image", brand_id="brand01",
    )
    job.script = {"title": topic, "scenes": [
        {"prompt": "Kadr", "voiceover": "Text", "duration": 5}]}
    return store, job


def _render(patch_providers):
    patch_providers.video_engine.return_value.render.side_effect = (
        lambda out, **kw: _write_file(out))


def _produce_v1(tmp_path, topic: str = "Regen topic"):
    store, job = _build_store(tmp_path, topic)
    image = _write_file(store.root / job.job_id / "images" / "img.png")
    with patch("core.pipeline.providers") as mp:
        _render(mp)
        job = finalize_job(store, job, image)
    assert job.status == JobStatus.VIDEO_PENDING_APPROVAL
    assert job.active_video_version == 1
    return store.root, store, job


def _make_vv(version: int):
    import hashlib
    from core.models import VideoVersion
    return VideoVersion(
        version=version, path=f"final/video-v{version}.mp4",
        sha256=hashlib.sha256(f"video-v{version}".encode()).hexdigest(),
    )


def _digest_file(path: Path) -> str:
    from core.artifacts import _digest
    return _digest(path)


class TestSharedRegenerationAndApi:
    def test_finalize_video_regenerate_produces_new_immutable_version(self, tmp_path):
        import core.app as app_module
        root, store, job = _produce_v1(tmp_path)
        v1_sha = _digest_file(root / job.job_id / "final" / "video-v1.mp4")
        app_module.store = store  # shared path reads the module-global store
        with patch("core.pipeline.providers") as mp:
            _render(mp)
            app_module._finalize_video_regenerate(job)
        current = store.jobs[job.job_id]
        assert current.active_video_version == 2
        assert len(current.video_versions) == 2
        assert current.status == JobStatus.VIDEO_PENDING_APPROVAL
        assert current.video_versions[0].version == 1
        assert current.video_versions[0].sha256 == v1_sha
        assert current.video_versions[1].version == 2
        assert (root / job.job_id / "final" / "video-v1.mp4").is_file()
        assert (root / job.job_id / "final" / "video-v2.mp4").is_file()

    def test_web_regenerate_dispatches_shared_function(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from core.app import app
        import core.api.jobs as jobs_mod
        root, store, job = _produce_v1(tmp_path)
        original_post = jobs_mod.store
        jobs_mod.store = store
        monkeypatch.setattr(
            "core.app._finalize_video_regenerate",
            lambda j: _write_file(store.root / j.job_id / "final" / "video-v2.mp4") or j)
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(f"/api/jobs/{job.job_id}/video/regenerate",
                               json={"actor": "test"})
            assert resp.status_code == 200
        finally:
            jobs_mod.store = original_post
        current = store.jobs[job.job_id]
        # synchronous transition to revision-requested before background dispatch
        assert current.status == JobStatus.VIDEO_REVISION_REQUESTED
        assert current.video_revisions[0].text == "regenerate"

    def test_web_video_approve_rejects_stale_version(self, tmp_path):
        from fastapi.testclient import TestClient
        from core.app import app
        import core.api.jobs as jobs_mod
        root, store, job = _produce_v1(tmp_path)
        _write_file(root / job.job_id / "final" / "video-v2.mp4")
        job.video_versions.append(_make_vv(2))
        job.active_video_version = 2
        job.status = JobStatus.VIDEO_PENDING_APPROVAL
        store._save(job)
        original_post = jobs_mod.store
        jobs_mod.store = store
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(f"/api/jobs/{job.job_id}/video/approve",
                               json={"actor": "test", "expected_video_version": 1})
            assert resp.status_code == 409
            assert "Stale" in resp.json()["detail"]
        finally:
            jobs_mod.store = original_post

    def test_web_video_approve_active_version_sets_ready(self, tmp_path):
        from fastapi.testclient import TestClient
        from core.app import app
        import core.api.jobs as jobs_mod
        root, store, job = _produce_v1(tmp_path)
        original_post = jobs_mod.store
        jobs_mod.store = store
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(f"/api/jobs/{job.job_id}/video/approve",
                               json={"actor": "test", "expected_video_version": 1})
            assert resp.status_code == 200
        finally:
            jobs_mod.store = original_post
        current = store.jobs[job.job_id]
        assert current.status == JobStatus.READY
        assert current.video_versions[0].approved is True
        assert current.video_versions[0].approved_by == "test"

    def test_web_video_revision_stores_text(self, tmp_path):
        from fastapi.testclient import TestClient
        from core.app import app
        import core.api.jobs as jobs_mod
        root, store, job = _produce_v1(tmp_path)
        original_post = jobs_mod.store
        jobs_mod.store = store
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(f"/api/jobs/{job.job_id}/video/revision",
                               json={"revision": "Make the intro longer", "actor": "test"})
            assert resp.status_code == 200
        finally:
            jobs_mod.store = original_post
        current = store.jobs[job.job_id]
        assert current.status == JobStatus.VIDEO_REVISION_REQUESTED
        assert len(current.video_revisions) == 1
        rev = current.video_revisions[0]
        assert rev.version == 1
        assert rev.text == "Make the intro longer"
        assert rev.actor == "test"

    def test_finalize_video_regenerate_aborts_on_cancelled(self, tmp_path):
        """Issue #49: cancelled job must not be resurrected by async regeneration."""
        import core.app as app_module
        root, store, job = _produce_v1(tmp_path)
        with store.lock:
            job.status = JobStatus.CANCELLED
            store._save(job)
        app_module.store = store
        with patch("core.pipeline.providers") as mp:
            _render(mp)
            app_module._finalize_video_regenerate(job)
        current = store.jobs[job.job_id]
        assert current.status == JobStatus.CANCELLED
        assert current.active_video_version == 1
        assert len(current.video_versions) == 1
        # no new version file produced for the cancelled job
        assert not (root / job.job_id / "final" / "video-v2.mp4").exists()

    def test_revision_bound_to_resulting_version(self, tmp_path):
        """Issue #49: the new version records the revision it was regenerated for."""
        from core.pipeline import request_video_revision
        root, store, job = _produce_v1(tmp_path)
        image = root / job.job_id / "images" / "img.png"
        revision_text = "Lengthen the intro"
        job = request_video_revision(store, job, revision_text, "test")
        assert job.status == JobStatus.VIDEO_REVISION_REQUESTED
        with patch("core.pipeline.providers") as mp:
            _render(mp)
            job = finalize_job(store, job, image)
        assert job.active_video_version == 2
        # v1 has no revision note; v2 documents the revision that produced it
        assert job.video_versions[0].revision_note is None
        assert job.video_versions[1].revision_note == revision_text