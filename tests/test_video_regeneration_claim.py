"""Issue #82 — persistence-safe regeneration attempt lease and render fencing.

A regeneration attempt is a *lease*, not a state: it is claimed atomically
before dispatch and released on every exit path. These tests use the real
disk-backed ``FileRepository`` (no mock store) and prove:

- a successful render releases the claim, so the next attempt is admitted;
- a concurrent second claim is refused while a render is in flight;
- the claim is persisted and released by a CORE restart;
- a cancel accepted *during* the render discards the output and registers no
  version, instead of resurrecting the cancelled Job;
- the Web endpoint releases the claim when dispatch itself fails.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.artifacts import register_artifact
from core.models import JobStatus, SceneRecord
from core.pipeline import (JobStore, approve_video, claim_video_regeneration,
                           finalize_job, release_video_regeneration,
                           request_video_revision)
from core.repository import FileRepository


def _write_file(path: Path, content: bytes = b"fake video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _build_store(tmp_path, topic: str = "Claim topic") -> tuple[JobStore, object]:
    root = tmp_path / "jobs"
    store = JobStore(root=str(root), repository=FileRepository(root))
    job = store.create(topic=topic, character_id="test_char", priority=5,
                       source="web", task_type="image", brand_id="brand01")
    job.script = {"title": topic, "scenes": [
        {"prompt": "Kadr", "voiceover": "Text", "duration": 5}]}
    return store, job


def _render(mp, on_render=None):
    def _side_effect(out, **kwargs):
        _write_file(out)
        if on_render is not None:
            on_render()
    mp.video_engine.return_value.render.side_effect = _side_effect


def _register_scene_image(store, job) -> Path:
    image = _write_file(store.root / job.job_id / "images" / "scene-001.png")
    record = register_artifact(job, store.root, image, "image", scene_id="scene-001")
    job.scenes.append(SceneRecord(scene_id="scene-001", index=1, prompt="Kadr",
                                  artifact_ids=[record.artifact_id]))
    store._save(job)
    return image


def _produce_v1(tmp_path):
    store, job = _build_store(tmp_path)
    image = _write_file(store.root / job.job_id / "images" / "img.png")
    with patch("core.pipeline.providers") as mp:
        _render(mp)
        job = finalize_job(store, job, image)
    assert job.status == JobStatus.VIDEO_PENDING_APPROVAL
    assert job.active_video_version == 1
    return tmp_path / "jobs", store, job


class TestClaimLease:
    def test_successful_render_releases_claim(self, tmp_path):
        import core.app as app_module
        root, store, job = _produce_v1(tmp_path)
        _register_scene_image(store, job)
        app_module.store = store
        claim_video_regeneration(store, job, "test")
        assert job.video_regenerating is True
        with patch("core.pipeline.providers") as mp:
            _render(mp)
            app_module._finalize_video_regenerate(job)
        current = store.jobs[job.job_id]
        assert current.active_video_version == 2
        # the lease is released, so a further attempt is admissible again
        assert current.video_regenerating is False
        job = request_video_revision(store, current, "regenerate", "test")
        assert job.status == JobStatus.VIDEO_REVISION_REQUESTED
        claim_video_regeneration(store, job, "test")
        assert job.video_regenerating is True

    def test_second_concurrent_claim_is_refused(self, tmp_path):
        _, store, job = _produce_v1(tmp_path)
        claim_video_regeneration(store, job, "first")
        with pytest.raises(ValueError, match="already in progress"):
            claim_video_regeneration(store, job, "second")
        release_video_regeneration(store, job)
        claim_video_regeneration(store, job, "third")
        assert job.video_regenerating is True

    def test_release_is_idempotent(self, tmp_path):
        _, store, job = _produce_v1(tmp_path)
        release_video_regeneration(store, job)
        claim_video_regeneration(store, job, "test")
        release_video_regeneration(store, job)
        release_video_regeneration(store, job)
        assert job.video_regenerating is False

    def test_claim_refuses_cancelled_job(self, tmp_path):
        _, store, job = _produce_v1(tmp_path)
        job.status = JobStatus.CANCELLED
        with pytest.raises(ValueError, match="CANCELLED"):
            claim_video_regeneration(store, job, "test")
        assert job.video_regenerating is False

    def test_restart_releases_stale_claim(self, tmp_path):
        root, store, job = _produce_v1(tmp_path)
        claim_video_regeneration(store, job, "test")
        assert job.video_regenerating is True
        # the claim is durable: a fresh process sees it before recovery
        restarted = JobStore(root=str(root), repository=FileRepository(root))
        loaded = restarted.jobs[job.job_id]
        assert loaded.video_regenerating is False
        assert any("STALE VIDEO REGENERATION CLAIM RELEASED" in event
                   for event in loaded.events)
        # and the released claim is persisted, not only in memory
        reloaded = JobStore(root=str(root), repository=FileRepository(root))
        assert reloaded.jobs[job.job_id].video_regenerating is False

    def test_web_endpoint_releases_claim_when_dispatch_fails(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from core.app import app
        import core.api.jobs as jobs_mod
        root, store, job = _produce_v1(tmp_path)
        original_store = jobs_mod.store
        jobs_mod.store = store

        class _Boom:
            def submit(self, *args, **kwargs):
                raise RuntimeError("executor unavailable")

        monkeypatch.setattr(jobs_mod, "executor", _Boom())
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(f"/api/jobs/{job.job_id}/video/regenerate",
                               json={"actor": "test"})
            assert resp.status_code == 500
        finally:
            jobs_mod.store = original_store
        current = store.jobs[job.job_id]
        assert current.video_regenerating is False


class TestRenderFencing:
    def test_cancel_during_render_registers_no_version(self, tmp_path):
        import core.app as app_module
        root, store, job = _produce_v1(tmp_path)
        _register_scene_image(store, job)

        def _cancel_mid_render():
            with store.lock:
                store.jobs[job.job_id].status = JobStatus.CANCELLED
                store._save(store.jobs[job.job_id])

        app_module.store = store
        claim_video_regeneration(store, job, "test")
        with patch("core.pipeline.providers") as mp:
            _render(mp, on_render=_cancel_mid_render)
            app_module._finalize_video_regenerate(job)
        current = store.jobs[job.job_id]
        # cancelled Job is not resurrected, no version v2 exists, claim released
        assert current.status == JobStatus.CANCELLED
        assert current.active_video_version == 1
        assert len(current.video_versions) == 1
        assert current.video_regenerating is False
        assert not (root / job.job_id / "final" / "video-v2.mp4").exists()
        assert any("ASSEMBLY RESULT DISCARDED" in event for event in current.events)

    def test_pause_during_render_registers_no_version(self, tmp_path):
        import core.app as app_module
        root, store, job = _produce_v1(tmp_path)
        _register_scene_image(store, job)

        def _pause_mid_render():
            with store.lock:
                store.jobs[job.job_id].status = JobStatus.PAUSED
                store._save(store.jobs[job.job_id])

        app_module.store = store
        claim_video_regeneration(store, job, "test")
        with patch("core.pipeline.providers") as mp:
            _render(mp, on_render=_pause_mid_render)
            app_module._finalize_video_regenerate(job)
        current = store.jobs[job.job_id]
        assert current.status == JobStatus.PAUSED
        assert current.active_video_version == 1
        assert current.video_regenerating is False
        assert not (root / job.job_id / "final" / "video-v2.mp4").exists()

    def test_approved_version_still_restores_after_claim_cycle(self, tmp_path):
        root, store, job = _produce_v1(tmp_path)
        job = approve_video(store, job, "test", expected_version=1)
        assert job.status == JobStatus.READY
        restarted = JobStore(root=str(root), repository=FileRepository(root))
        loaded = restarted.jobs[job.job_id]
        assert loaded.status == JobStatus.READY
        assert loaded.active_video_version == 1
        assert loaded.video_versions[0].approved is True
        assert loaded.video_regenerating is False
