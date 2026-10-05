"""Issue #82 вЂ” disposable First Runв†’Text/Voice/GPU/Publisher acceptance harness.

Every test drives the real control plane: the FastAPI app, the real task queue
with leases, the real CORE result/receipt boundaries and the real disk-backed
persistence. Only generators and external providers are replaced (LLM, GPU
compute, TTS HTTP runtime, FFmpeg binary, publishing platform) вЂ” see
``tests/media_harness.py``. No test sets a job status directly to reach a
conclusion.

Declared acceptance rows (also consumed by ``scripts/media-acceptance-report.py``):

* ``first_run``      вЂ” First Runв†’CORE/storage/queueв†’controlled Text/Voice/GPU/Publisher
* ``approval_loops`` вЂ” script/storyboard/video approval and two video revision loops
* ``telegram_flow``  вЂ” Telegram-sourced Job, chat-bound approval and sandbox receipt
* ``partial_publish``вЂ” partial publish receipts, bounded retry, receipt correlation
* ``dedup``          вЂ” duplicate regeneration refused while a render claim is held
* ``cancel``         вЂ” cancel during render discards the result, no version registered
* ``stale_result``   вЂ” late worker result after cancel is refused
* ``lease``          вЂ” expired lease requeues the task and fences the stale result
* ``restart``        вЂ” restart replays the Job, releases a stale claim, keeps approval
* ``backup_update``  вЂ” backup snapshot receipt import and update-agent smoke
* ``evidence``       вЂ” CI evidence: SHA-anchored artifacts and receipts
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.models import JobStatus
from core.state import store, task_queue
from tests import media_harness as harness


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def fleet(client, monkeypatch, tmp_path):
    """Disposable installation: characters, node fleet and generator stubs."""
    harness.reset_counters()
    harness.register_fleet(client, characters_root=tmp_path / "characters",
                           character_id="harnesschar", monkeypatch=monkeypatch)
    return client


def _run_to_video(client, monkeypatch, job_id: str, *, revision: str | None = None,
                  min_version: int = 1) -> dict:
    """Script в†’ storyboard в†’ images в†’ TTS в†’ render, through the real boundaries."""
    harness.approve_script(client, job_id, revision=revision)
    harness.generate_assets(client, job_id, monkeypatch)
    return harness.wait_for_video(client, job_id, min_version=min_version)


class TestFirstRunAcceptance:
    def test_first_run_reaches_published_with_real_receipt(self, fleet, monkeypatch, tmp_path):
        """First Runв†’Textв†’Voiceв†’GPUв†’Publisher produces a correlated sandbox receipt."""
        job_id = harness.create_job(fleet, topic="First run topic")
        _run_to_video(fleet, monkeypatch, job_id)

        job = harness.approve_video(fleet, job_id, version=1)
        assert job["status"] == "READY"
        assert job["active_video_version"] == 1

        harness.register_publisher(fleet)
        response = fleet.post(f"/api/jobs/{job_id}/publish", json=["youtube"])
        assert response.status_code == 200, response.text
        task = harness.claim_publish(fleet, job_id, channel="youtube")
        receipt = harness.sandbox_receipt("youtube", task)
        harness.submit_publish_receipt(fleet, job_id, task, receipt)

        job = harness.get_job(fleet, job_id)
        assert "youtube" in job["published_to"], job
        recorded = job["publication_results"]["youtube"]
        assert recorded["status"] == "PUBLISHED"
        assert recorded["remote_id"] == "mock-youtube"
        # the receipt is correlated with the durable delivery contract
        assert recorded["video_version"] == 1
        evidence = harness.video_evidence(job_id, job)
        harness.record_evidence("first_run", {"video": evidence,
                                              "receipts": job["publication_results"]})
        assert evidence["files"], "the rendered version must exist on disk"

    def test_telegram_sourced_job_is_approved_and_published(self, fleet, monkeypatch):
        """A Telegram Job keeps its owner, is reviewed in Web and published with a receipt."""
        job_id = harness.create_telegram_job(topic="Telegram topic", chat_id="424242")
        assert store.jobs[job_id].source == "telegram:424242"
        _run_to_video(fleet, monkeypatch, job_id)

        # a foreign chat may not act on this Job's video
        from unittest.mock import patch
        import core.app as app_module
        answers = []
        adapter = type("Adapter", (), {
            "answer_callback": lambda self, callback_id, text=None, **kw: answers.append(text),
            "send_message": lambda self, chat_id, text, markup=None: None,
        })()
        with patch.object(app_module, "TelegramAdapter", lambda: adapter):
            app_module._handle_video_callback(
                {"id": "cb-foreign", "message": {"chat": {"id": "999"}}},
                "999", "vid_ok", f"{job_id}:1")
        assert answers and "заборонено" in answers[-1].lower()
        assert harness.get_job(fleet, job_id)["status"] == "VIDEO_PENDING_APPROVAL"

        job = harness.approve_video(fleet, job_id, version=1)
        assert job["status"] == "READY"
        harness.register_publisher(fleet)
        assert fleet.post(f"/api/jobs/{job_id}/publish",
                          json=["telegram"]).status_code == 200


class TestVideoRevisionLoops:
    def test_two_revision_loops_keep_immutable_versions_across_restart(
            self, fleet, monkeypatch):
        """Free-text revision loop, then pure re-render loop, both reviewed and approved."""
        job_id = harness.create_job(fleet, topic="Revision loops")
        _run_to_video(fleet, monkeypatch, job_id)
        job = harness.get_job(fleet, job_id)
        v1 = dict(job["video_versions"][0])
        assert v1["approved"] is False

        # --- loop 1: free text travels upstream through script and storyboard ---
        harness.request_video_revision(fleet, job_id, "make the intro longer")
        harness.wait_for_status(fleet, job_id, "SCRIPT_REVISION_REQUESTED",
                                "SCRIPT_QUEUED")
        harness.approve_script(fleet, job_id, revision=None)
        harness.generate_assets(fleet, job_id, monkeypatch)
        job = harness.wait_for_video(fleet, job_id, min_version=2)
        assert job["active_video_version"] == 2
        assert job["video_versions"][1]["revision_note"] == "make the intro longer"
        assert job["status"] == "VIDEO_PENDING_APPROVAL"

        # --- restart between the loops: approval and versions must survive ---
        restarted = harness.restart_store()
        loaded = restarted.jobs[job_id]
        assert loaded.status == JobStatus.VIDEO_PENDING_APPROVAL
        assert loaded.active_video_version == 2
        assert [v.approved for v in loaded.video_versions] == [False, False]

        # --- loop 2: pure re-render of the approved inputs ---
        job = harness.regenerate_video(fleet, job_id, min_version=3)
        assert job["active_video_version"] == 3
        assert len(job["video_versions"]) == 3
        job = harness.approve_video(fleet, job_id, version=3)
        assert job["status"] == "READY"

        files = harness.version_paths(job_id)
        assert [path.name for path in files] == [
            "video-v1.mp4", "video-v2.mp4", "video-v3.mp4"]
        hashes = harness.artifact_hashes(files)
        assert len(set(hashes.values())) == 3, "each render must be a distinct artifact"
        recorded = [version["sha256"] for version in job["video_versions"]]
        assert recorded == [hashes[str(path)] for path in files]
        # the approved state is per version, never overwritten
        assert [version["approved"] for version in job["video_versions"]] == [False, False, True]

        harness.record_evidence("approval_loops", {
            "video": harness.video_evidence(job_id, job),
            "loops": 2,
        })
