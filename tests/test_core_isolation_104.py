"""Tests for core/local_fallback.py — Issue i.0.0.1.4 (#104)."""

from __future__ import annotations

from core.local_fallback import local_fallback_allowed, require_no_local_execution


class TestLocalFallbackGuard:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("LOCAL_WORKER_FALLBACK", raising=False)
        monkeypatch.delenv("VERTEP_DEMO", raising=False)
        assert local_fallback_allowed() is False

    def test_fallback_alone_is_not_enough(self, monkeypatch):
        monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")
        monkeypatch.delenv("VERTEP_DEMO", raising=False)
        assert local_fallback_allowed() is False

    def test_demo_alone_is_not_enough(self, monkeypatch):
        monkeypatch.delenv("LOCAL_WORKER_FALLBACK", raising=False)
        monkeypatch.setenv("VERTEP_DEMO", "true")
        assert local_fallback_allowed() is False

    def test_explicit_dev_demo_enables(self, monkeypatch):
        monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")
        monkeypatch.setenv("VERTEP_DEMO", "true")
        assert local_fallback_allowed() is True

    def test_require_raises_outside_demo(self, monkeypatch):
        import pytest
        monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")
        monkeypatch.delenv("VERTEP_DEMO", raising=False)
        with pytest.raises(RuntimeError, match="dev/demo only"):
            require_no_local_execution("script")

    def test_finalize_job_refuses_foreign_engine(self, monkeypatch, tmp_path):
        """CORE finalize_job must not render a non-native engine (i.0.0.1.4 #104)."""
        from core.models import Job, JobStatus, utc_now
        from core.pipeline import JobStore, finalize_job, _is_native_engine
        from core.repository import FileRepository

        monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
        monkeypatch.delenv("VERTEP_DEMO", raising=False)
        # Unit-level guard proofs.
        class _Foreign:
            engine_id = None
            provider = "money-printer"

        class _Native:
            engine_id = "native"
            provider = "native"

        assert _is_native_engine(_Native()) is True
        assert _is_native_engine(_Foreign()) is False
        assert _is_native_engine(object()) is True  # no identity claims → native route

        store = JobStore(root=tmp_path / "jobs", repository=FileRepository(tmp_path / "repo.json"))
        job = Job(job_id="job-104", topic="Isolation", character_id="c", priority=5,
                  status=JobStatus.NEW, created_at=utc_now(), task_type="image")
        store.jobs[job.job_id] = job
        image = tmp_path / "img.ppm"
        image.write_bytes(b"P6\n2 2\n255\n" + bytes((10, 20, 30)) * 4)

        calls = {"render": 0, "enqueue": 0}

        class _Engine:
            engine_id = None
            provider = "shortgpt"

            def render(self, *a, **k):
                calls["render"] += 1

        import core.pipeline as _pipeline
        monkeypatch.setattr(_pipeline, "providers",
                            type("_P", (), {"video_engine": staticmethod(lambda: _Engine())}))
        import core.api.job_helpers as _jh
        monkeypatch.setattr(_jh, "_enqueue_assembly_task",
                            lambda j, t: calls.__setitem__("enqueue", calls["enqueue"] + 1))

        finalize_job(store, job, image)
        assert calls["render"] == 0, "a foreign engine must never render in CORE"
        assert calls["enqueue"] == 0, "an unidentified engine is refused, not dispatched"
        assert job.status is JobStatus.FAILED
        assert any("IS NOT NATIVE" in event for event in job.events)
