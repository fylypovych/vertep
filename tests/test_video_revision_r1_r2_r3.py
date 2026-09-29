"""Automated evidence for Issue #81: video revision contract (R1/R2/R3).

R1 — a free-text video revision is applied to generation: the Job leaves the
     video approval stage, regenerates the script and carries the same text to
     the storyboard regeneration that follows; the upstream text survives a
     restart and is retired by the render it produced.
R2 — re-assembly consumes registered, scene-ordered, integrity-checked
     artifacts instead of globbing historical ``images/``/``storyboard/`` data.
R3 — video approval is bound to the reviewed version and its SHA256 (HTTP body
     requires the version, the reviewed hash is verified against the immutable
     record and the file on disk), and every video action is chat-bound.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from core.artifacts import _digest, register_artifact
from core.models import JobStatus, SceneRecord
from core.pipeline import JobStore, finalize_job
from core.repository import FileRepository


def _write(path: Path, content: bytes = b"image-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _build_store(tmp_path: Path) -> tuple[JobStore, object]:
    root = tmp_path / "jobs"
    store = JobStore(root=str(root), repository=FileRepository(root))
    job = store.create(topic="Issue 81", character_id="test_char", priority=5,
                       source="web", task_type="image", brand_id="brand01")
    job.script = {"title": "Issue 81", "scenes": [
        {"prompt": "Kadr", "voiceover": "Text", "duration": 5}]}
    store._save(job)
    return store, job


def _render_v1(store: JobStore, job):
    image = _write(store.root / job.job_id / "images" / "img.png")
    with patch("core.pipeline.providers") as mp:
        mp.video_engine.return_value.render.side_effect = lambda out, **kw: _write(out, b"v1")
        finalize_job(store, job, image)
    assert job.status == JobStatus.VIDEO_PENDING_APPROVAL
    assert job.active_video_version == 1
    return job


def _register_scene(store: JobStore, job, index: int, filename: str,
                    kind: str = "image", content: bytes = b"scene-bytes") -> Path:
    path = _write(store.root / job.job_id / "images" / filename, content)
    record = register_artifact(job, store.root, path, kind, scene_id=f"scene-{index}")
    job.scenes.append(SceneRecord(scene_id=f"scene-{index}", index=index, prompt="Kadr",
                                  artifact_ids=[record.artifact_id]))
    return path


# ---------------------------------------------------------------------------
# R1 — free-text video revision drives generation
# ---------------------------------------------------------------------------

class TestR1VideoRevisionDrivesGeneration:
    def test_free_text_revision_routes_upstream_and_persists(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.api.job_helpers._enqueue_script_task",
                            lambda *args, **kwargs: None)
        from core.pipeline import request_video_revision
        store, job = _build_store(tmp_path)
        _render_v1(store, job)

        job = request_video_revision(store, job, "Make it brighter", "user")
        assert job.status == JobStatus.SCRIPT_QUEUED
        assert job.video_revision_upstream == "Make it brighter"
        assert job.approval_status == "revision_requested"
        assert len(job.video_revisions) == 1
        assert job.video_revisions[0].version == 1

        restarted = JobStore(root=str(store.root), repository=FileRepository(store.root))
        loaded = restarted.jobs[job.job_id]
        assert loaded.video_revision_upstream == "Make it brighter"
        assert loaded.video_revisions[-1].text == "Make it brighter"

    def test_regeneration_sentinel_keeps_pure_rerender_loop(self, tmp_path):
        from core.pipeline import request_video_revision
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        job = request_video_revision(store, job, "regenerate", "user")
        assert job.status == JobStatus.VIDEO_REVISION_REQUESTED
        assert job.video_revision_upstream is None
        assert job.video_revisions[-1].text == "regenerate"

    def test_empty_revision_text_is_rejected(self, tmp_path):
        from core.pipeline import request_video_revision
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        with pytest.raises(ValueError, match="required"):
            request_video_revision(store, job, "   ", "user")
        assert job.status == JobStatus.VIDEO_PENDING_APPROVAL
        assert job.video_revisions == []

    def test_prepare_and_dispatch_forwards_upstream_text_to_storyboard(self, tmp_path, monkeypatch):
        import core.api.job_helpers as helpers
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        job.status = JobStatus.SCRIPT_APPROVED
        job.video_revision_upstream = "Make it brighter"
        captured: list[object] = []
        monkeypatch.setattr(helpers, "store", store)
        monkeypatch.setattr(helpers, "queue_storyboard",
                            lambda st, jb, revision=None: captured.append(revision))
        helpers._prepare_and_dispatch(job)
        assert captured == ["Make it brighter"]

    def test_prepare_and_dispatch_without_upstream_keeps_default(self, tmp_path, monkeypatch):
        import core.api.job_helpers as helpers
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        job.status = JobStatus.SCRIPT_APPROVED
        captured: list[object] = []
        monkeypatch.setattr(helpers, "store", store)
        monkeypatch.setattr(helpers, "queue_storyboard",
                            lambda st, jb, revision=None: captured.append(revision))
        helpers._prepare_and_dispatch(job)
        assert captured == [None]

    def test_render_retires_upstream_text_and_binds_revision_note(self, tmp_path):
        from core.pipeline import request_video_revision
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        job = request_video_revision(store, job, "Make it brighter", "user")
        # upstream regeneration completed: script is back, artifacts registered
        job.script = {"title": "Issue 81", "scenes": [
            {"prompt": "Kadr", "voiceover": "Text", "duration": 5}]}
        scene_image = _register_scene(store, job, 1, "frame.jpg")
        with patch("core.pipeline.providers") as mp:
            mp.video_engine.return_value.render.side_effect = lambda out, **kw: _write(out, b"v2")
            finalize_job(store, job, scene_image)
        assert job.active_video_version == 2
        assert job.video_versions[-1].revision_note == "Make it brighter"
        assert job.video_revision_upstream is None


# ---------------------------------------------------------------------------
# R2 — registered, ordered, integrity-checked re-assembly inputs
# ---------------------------------------------------------------------------

class TestR2RegisteredOrderedIntegrityInputs:
    def test_scene_index_order_wins_over_lexicographic_names(self, tmp_path, monkeypatch):
        from core.app import _collect_reassembly_inputs
        store, job = _build_store(tmp_path)
        monkeypatch.setattr("core.app.store", store)
        for index, name in ((1, "sb-1-1.png"), (2, "sb-1-10.png"), (10, "sb-1-2.png")):
            _register_scene(store, job, index, name)
        store._save(job)
        # lexicographic order would be 1, 10, 2 — scene index must win
        assert [p.name for p in _collect_reassembly_inputs(job)] == [
            "sb-1-1.png", "sb-1-10.png", "sb-1-2.png"]

    def test_non_png_and_newest_artifact_per_scene_are_used(self, tmp_path, monkeypatch):
        from core.app import _collect_reassembly_inputs
        store, job = _build_store(tmp_path)
        monkeypatch.setattr("core.app.store", store)
        _register_scene(store, job, 1, "frame.webp")
        # a scene regenerated later keeps its older artifact as history
        _register_scene(store, job, 1, "frame-v2.jpg")
        store._save(job)
        assert [p.name for p in _collect_reassembly_inputs(job)] == ["frame-v2.jpg"]

    def test_video_task_reads_video_scene_clips(self, tmp_path, monkeypatch):
        from core.app import _collect_reassembly_inputs
        store, job = _build_store(tmp_path)
        monkeypatch.setattr("core.app.store", store)
        job.task_type = "video"
        _register_scene(store, job, 1, "clip.mp4", kind="video_scene")
        _register_scene(store, job, 2, "ignored.png", kind="image")
        store._save(job)
        assert [p.name for p in _collect_reassembly_inputs(job)] == ["clip.mp4"]

    def test_corrupted_artifact_is_reported_and_skipped(self, tmp_path, monkeypatch):
        from core.app import _collect_reassembly_inputs
        store, job = _build_store(tmp_path)
        monkeypatch.setattr("core.app.store", store)
        _register_scene(store, job, 1, "good.png")
        corrupted = _register_scene(store, job, 2, "bad.png")
        corrupted.write_bytes(b"tampered")
        store._save(job)
        inputs = _collect_reassembly_inputs(job)
        assert [p.name for p in inputs] == ["good.png"]
        assert any("RE-ASSEMBLY ARTIFACTS REJECTED" in line for line in job.events)

    def test_no_valid_artifacts_fails_video_explicitly(self, tmp_path):
        import core.app as app_module
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        corrupted = _register_scene(store, job, 1, "bad.png")
        corrupted.write_bytes(b"tampered")
        store._save(job)
        app_module.store = store
        with patch("core.pipeline.providers"):
            app_module._finalize_video_regenerate(job)
        current = store.jobs[job.job_id]
        assert current.status == JobStatus.VIDEO_FAILED
        assert current.active_video_version == 1
        assert any("RE-ASSEMBLY ARTIFACTS REJECTED" in line for line in current.events)

    def test_missing_artifact_file_is_reported(self, tmp_path, monkeypatch):
        from core.app import _collect_reassembly_inputs
        store, job = _build_store(tmp_path)
        monkeypatch.setattr("core.app.store", store)
        path = _register_scene(store, job, 1, "gone.png")
        path.unlink()
        store._save(job)
        assert _collect_reassembly_inputs(job) == []
        assert any("RE-ASSEMBLY ARTIFACTS REJECTED" in line for line in job.events)


# ---------------------------------------------------------------------------
# R3 — approval bound to reviewed version and hash
# ---------------------------------------------------------------------------

class TestR3ApprovalBoundToReviewedVersion:
    def _approved_store(self, tmp_path):
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        return store, job

    def test_correct_version_and_hash_approve(self, tmp_path):
        from core.pipeline import approve_video
        store, job = self._approved_store(tmp_path)
        reviewed = job.video_versions[-1]
        file_path = store.root / job.job_id / reviewed.path
        job = approve_video(store, job, "web", expected_version=1, expected_sha256=reviewed.sha256)
        assert job.status == JobStatus.READY
        assert job.video_versions[-1].approved is True
        assert _digest(file_path) == reviewed.sha256

    def test_stale_hash_is_rejected(self, tmp_path):
        from core.pipeline import approve_video
        store, job = self._approved_store(tmp_path)
        with pytest.raises(ValueError, match="Stale"):
            approve_video(store, job, "web", expected_version=1,
                          expected_sha256="0" * 64)

    def test_missing_reviewed_file_is_rejected(self, tmp_path):
        from core.pipeline import approve_video
        store, job = self._approved_store(tmp_path)
        reviewed = job.video_versions[-1]
        (store.root / job.job_id / reviewed.path).unlink()
        with pytest.raises(ValueError, match="missing"):
            approve_video(store, job, "web", expected_version=1,
                          expected_sha256=reviewed.sha256)

    def test_tampered_reviewed_file_is_rejected(self, tmp_path):
        from core.pipeline import approve_video
        store, job = self._approved_store(tmp_path)
        reviewed = job.video_versions[-1]
        (store.root / job.job_id / reviewed.path).write_bytes(b"tampered")
        with pytest.raises(ValueError, match="integrity"):
            approve_video(store, job, "web", expected_version=1,
                          expected_sha256=reviewed.sha256)

    def test_stale_version_approval_is_rejected(self, tmp_path):
        from core.pipeline import approve_video
        store, job = self._approved_store(tmp_path)
        with pytest.raises(ValueError, match="Stale"):
            approve_video(store, job, "web", expected_version=2)

    def test_second_approval_after_ready_is_rejected(self, tmp_path):
        from core.pipeline import approve_video
        store, job = self._approved_store(tmp_path)
        approve_video(store, job, "web", expected_version=1)
        with pytest.raises(ValueError, match="Cannot approve"):
            approve_video(store, job, "web", expected_version=1)

    def _client(self, store):
        from fastapi.testclient import TestClient
        from core.app import app
        return TestClient(app, raise_server_exceptions=False)

    def test_http_approve_requires_expected_version(self, tmp_path, monkeypatch):
        import core.api.jobs as jobs_mod
        store, job = self._approved_store(tmp_path)
        original = jobs_mod.store
        jobs_mod.store = store
        try:
            client = self._client(store)
            resp = client.post(f"/api/jobs/{job.job_id}/video/approve", json={"actor": "web"})
            assert resp.status_code == 422
        finally:
            jobs_mod.store = original

    def test_http_approve_binds_hash_and_version(self, tmp_path):
        import core.api.jobs as jobs_mod
        store, job = self._approved_store(tmp_path)
        reviewed = job.video_versions[-1]
        original = jobs_mod.store
        jobs_mod.store = store
        try:
            client = self._client(store)
            resp = client.post(f"/api/jobs/{job.job_id}/video/approve",
                               json={"actor": "web", "expected_video_version": 7})
            assert resp.status_code == 409
            resp = client.post(f"/api/jobs/{job.job_id}/video/approve",
                               json={"actor": "web", "expected_video_version": 1,
                                     "expected_sha256": "0" * 64})
            assert resp.status_code == 409
            resp = client.post(f"/api/jobs/{job.job_id}/video/approve",
                               json={"actor": "web", "expected_video_version": 1,
                                     "expected_sha256": reviewed.sha256})
            assert resp.status_code == 200
            assert resp.json()["status"] == "READY"
            resp = client.post(f"/api/jobs/{job.job_id}/video/approve",
                               json={"actor": "web", "expected_video_version": 1,
                                     "expected_sha256": reviewed.sha256})
            assert resp.status_code == 409
        finally:
            jobs_mod.store = original


class TestR3DoubleRequests:
    """A second approval or revision for the same stage must conflict."""

    def test_second_revision_request_is_rejected(self, tmp_path, monkeypatch):
        from core.pipeline import request_video_revision
        monkeypatch.setattr("core.api.job_helpers._enqueue_script_task",
                            lambda *args, **kwargs: None)
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        request_video_revision(store, job, "Make it brighter", "user")
        with pytest.raises(ValueError, match="Cannot request video revision"):
            request_video_revision(store, job, "Make it brighter again", "user")
        assert len(job.video_revisions) == 1
        assert job.video_revision_upstream == "Make it brighter"

    def test_http_double_revision_conflicts(self, tmp_path, monkeypatch):
        import core.api.jobs as jobs_mod
        from fastapi.testclient import TestClient
        from core.app import app
        monkeypatch.setattr("core.api.job_helpers._enqueue_script_task",
                            lambda *args, **kwargs: None)
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        original = jobs_mod.store
        jobs_mod.store = store
        try:
            client = TestClient(app, raise_server_exceptions=False)
            payload = {"actor": "web", "revision": "Make it brighter"}
            first = client.post(f"/api/jobs/{job.job_id}/video/revision", json=payload)
            assert first.status_code == 200
            second = client.post(f"/api/jobs/{job.job_id}/video/revision", json=payload)
            assert second.status_code == 409
        finally:
            jobs_mod.store = original


class TestVideoActionsAreChatBound:
    def _job(self, tmp_path):
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        job.source = "telegram:111"
        store._save(job)
        return store, job

    @pytest.mark.parametrize("action", ["vid_edit", "vid_regen", "vid_ok", "vid_reject"])
    def test_foreign_chat_is_denied(self, tmp_path, monkeypatch, action):
        from unittest.mock import Mock
        import core.app as app_module
        store, job = self._job(tmp_path)
        adapter = Mock()
        monkeypatch.setattr(app_module, "store", store)
        monkeypatch.setattr(app_module, "TelegramAdapter", lambda: adapter)
        job.status = JobStatus.VIDEO_REVISION_REQUESTED
        store._save(job)
        callback = {"id": "cb", "data": f"{action}:{job.job_id}",
                    "message": {"chat": {"id": "999"}}}
        app_module._handle_video_callback(callback, "999", action, job.job_id)
        assert adapter.answer_callback.called
        assert "Доступ заборонено" in adapter.answer_callback.call_args_list[-1][0][1]


class TestR3Concurrency:
    """Concurrent approval/revision requests must admit exactly one winner."""

    @staticmethod
    def _run_all(worker, count: int = 8) -> list[tuple[str, object]]:
        barrier = threading.Barrier(count)
        outcomes: list[tuple[str, object]] = []
        lock = threading.Lock()

        def attempt(index: int) -> None:
            barrier.wait()
            try:
                worker(index)
            except ValueError as error:
                with lock:
                    outcomes.append(("error", str(error)))
            else:
                with lock:
                    outcomes.append(("ok", index))

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return outcomes

    def test_concurrent_approvals_admit_exactly_one_winner(self, tmp_path):
        from core.pipeline import approve_video
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        with patch("core.pipeline._progress"):
            outcomes = self._run_all(
                lambda i: approve_video(store, job, "web", expected_version=1))
        winners = [item for item in outcomes if item[0] == "ok"]
        losers = [item for item in outcomes if item[0] == "error"]
        assert len(winners) == 1, outcomes
        assert len(losers) == len(outcomes) - 1
        assert all("Cannot approve video" in str(item[1]) for item in losers)
        assert job.status == JobStatus.READY
        assert job.approval_status == "approved"
        assert job.video_versions[0].approved is True
        assert job.video_versions[0].approved_by == "web"

    def test_concurrent_revision_requests_admit_exactly_one_route(self, tmp_path, monkeypatch):
        from core.pipeline import request_video_revision
        monkeypatch.setattr("core.api.job_helpers._enqueue_script_task",
                            lambda *args, **kwargs: None)
        store, job = _build_store(tmp_path)
        _render_v1(store, job)
        outcomes = self._run_all(
            lambda i: request_video_revision(store, job, f"Edit {i}", "user"))
        winners = [item for item in outcomes if item[0] == "ok"]
        assert len(winners) == 1, outcomes
        assert all("Cannot request video revision" in str(item[1])
                   for item in outcomes if item[0] == "error")
        assert len(job.video_revisions) == 1
        assert job.status == JobStatus.SCRIPT_QUEUED
        assert job.video_revision_upstream == f"Edit {winners[0][1]}"
