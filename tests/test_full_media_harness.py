"""Issue #82 -> disposable First Run->Text/Voice/GPU/Publisher acceptance harness.

Every test drives the real control plane: the FastAPI app, the real task queue
with leases, the real CORE result/receipt boundaries and the real disk-backed
persistence. Only generators and external providers are replaced (LLM, GPU
compute, TTS HTTP runtime, FFmpeg binary, publishing platform) -> see
``tests/media_harness.py``. No test sets a job status directly to reach a
conclusion.

Declared acceptance rows (also consumed by ``scripts/media-acceptance-report.py``):

* ``first_run``      -> First Run->CORE/storage/queue->controlled Text/Voice/GPU/Publisher
* ``approval_loops`` -> script/storyboard/video approval and two video revision loops
* ``telegram_flow``  -> Telegram-sourced Job, chat-bound approval and sandbox receipt
* ``failure``        -> bounded worker retry, then a clean terminal failure
* ``partial_publish``-> partial publish receipts, bounded retry, receipt correlation
* ``dedup``          -> duplicate regeneration refused while a render claim is held
* ``cancel``         -> cancel during render discards the result, no version registered
* ``stale_result``   -> late worker result after cancel is refused
* ``lease``          -> expired lease requeues the task and fences the stale result
* ``restart``        -> restart replays the Job, releases a stale claim, keeps approval
* ``backup_update``  -> backup snapshot receipt import and update-agent smoke
* ``evidence``       -> CI evidence: SHA-anchored artifacts and receipts
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.models import JobStatus
from core.state import store, task_queue
from tests import media_harness as harness


def test_review_goes_through_the_web_review_boundaries():
    """Issue #82 requires Web review through the standard boundaries.

    The harness drives the API directly, so every review endpoint it uses must be
    one the active Web UI actually calls. A private or invented endpoint would
    prove nothing about the product review surface.
    """
    import re

    used = set(re.findall(r"/api/jobs/\{job_id\}(/[A-Za-z0-9/_-]+)",
                          Path(harness.__file__).read_text(encoding="utf-8")))
    assert used, "the harness must drive the real Job review endpoints"
    api_dir = Path("web-v2/src/app/core/api")
    sources = "\n".join(path.read_text(encoding="utf-8") for path in api_dir.glob("*.ts"))
    web = set(re.findall(r"\}\s*(/[A-Za-z0-9/_-]+)", sources))
    assert used <= web, (
        "these review endpoints are not part of the Web UI review surface: "
        f"{sorted(used - web)}")


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
    """Script → storyboard → images → TTS → render, through the real boundaries."""
    harness.approve_script(client, job_id, monkeypatch, revision=revision)
    harness.generate_assets(client, job_id, monkeypatch)
    return harness.wait_for_video(client, job_id, min_version=min_version)


class _RenderGate:
    """Holds one render open so in-flight boundaries are observable from a test.

    Only the FFmpeg binary is replaced (allowed by the acceptance rules); the
    claim, cancel and release paths all stay real and run on the CORE executor
    thread while the test thread drives the HTTP boundaries.
    """

    def __init__(self, client, monkeypatch, tmp_path):
        self._armed = False
        self.rendering = threading.Event()
        self.release = threading.Event()
        harness.register_fleet(client, characters_root=tmp_path / "characters",
                               character_id="harnesschar", monkeypatch=monkeypatch,
                               on_render=self._on_render)

    def _on_render(self, output):
        if not self._armed:
            return
        self.rendering.set()
        if not self.release.wait(timeout=90):
            raise AssertionError("the render gate was never released")

    def arm(self) -> None:
        self._armed = True
        self.rendering.clear()
        self.release.clear()

    def wait_started(self) -> None:
        assert self.rendering.wait(timeout=90), "the held render never started"

    def open(self) -> None:
        self.release.set()
        self._armed = False

    def abandon(self) -> None:
        self._armed = False
        self.release.set()



class TestFirstRunAcceptance:
    def test_first_run_reaches_published_with_real_receipt(self, fleet, monkeypatch, tmp_path):
        """First Run->Text->Voice->GPU->Publisher produces a correlated sandbox receipt."""
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
        harness.record_row("first_run", "PUBLISHED", job["status"],
                           job_id=job_id,
                           video=evidence, receipts=job["publication_results"])
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
        task = harness.claim_publish(fleet, job_id, channel="telegram")
        harness.submit_publish_receipt(fleet, job_id, task,
                                       harness.sandbox_receipt("telegram", task))
        job = harness.get_job(fleet, job_id)
        assert job["status"] == "PUBLISHED"
        assert job["publication_results"]["telegram"]["video_version"] == 1
        harness.record_row("telegram_flow", "PUBLISHED", job["status"],
                           job_id=job_id,
                           source=job["source"],
                           chat_id=job["source"].split(":", 1)[1],
                           foreign_chat_refused=True,
                           receipt=job["publication_results"]["telegram"])


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
        harness.approve_script(fleet, job_id, monkeypatch, revision=None)
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

        harness.record_row("approval_loops", 2, len(job["video_versions"]) - 1,
                           job_id=job_id,
                           video=harness.video_evidence(job_id, job),
                           loops=2)


class TestBoundedFailure:
    def test_storyboard_failure_retries_inside_budget_then_fails_cleanly(self, fleet):
        """A failing Text Worker is retried a bounded number of times, never forever."""
        job_id = harness.create_job(fleet, topic="Failure budget")
        harness.wait_for_status(fleet, job_id, "SCRIPT_QUEUED")
        harness.run_script_task(fleet, job_id)
        harness.wait_for_status(fleet, job_id, "SCRIPT_PENDING_APPROVAL")
        assert fleet.post(f"/api/jobs/{job_id}/script/approve",
                          json={"actor": "harness"}).status_code == 200
        harness.wait_for_status(fleet, job_id, "STORYBOARD_QUEUED")

        budget = int(os.getenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3"))
        assert budget >= 2, "the retry budget must allow at least one retry"
        for attempt in range(1, budget):
            task = harness.claim_task(fleet, job_id, harness.TEXT_NODE, task="storyboard")
            response = fleet.post("/api/tasks/result", json={
                "job_id": job_id, "task_id": task["task_id"],
                "node_name": harness.TEXT_NODE, "success": False,
                "error": "ollama unavailable"})
            assert response.status_code == 200, response.text
            job = harness.get_job(fleet, job_id)
            assert job["status"] == "STORYBOARD_QUEUED", job["status"]
            assert store.jobs[job_id].storyboard_attempt == attempt

        # the attempt that exhausts the budget fails the Job cleanly
        task = harness.claim_task(fleet, job_id, harness.TEXT_NODE, task="storyboard")
        response = fleet.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": task["task_id"], "node_name": harness.TEXT_NODE,
            "success": False, "error": "ollama unavailable"})
        assert response.status_code == 400, response.text
        job = harness.get_job(fleet, job_id)
        assert job["status"] == "STORYBOARD_FAILED"
        assert store.jobs[job_id].storyboard_attempt == budget
        assert any(f"STORYBOARD FAILED after {budget} attempts" in line
                   for line in job["events"]), job["events"][-3:]
        # bounded: the failure is terminal, nothing is silently requeued for a
        # fourth attempt and no storyboard version was invented
        assert task_queue.depth() == 0
        assert store.jobs[job_id].storyboards == []
        harness.record_row("failure", "STORYBOARD_FAILED", job["status"],
                           job_id=job_id, status=job["status"], attempts=budget,
                           error="ollama unavailable")


class TestInFlightBoundaries:
    def test_duplicate_regeneration_is_refused_while_a_render_is_held(self, client,
                                                                    monkeypatch, tmp_path):
        """Two concurrent regenerations must never race into two new versions."""
        gate = _RenderGate(client, monkeypatch, tmp_path)
        job_id = harness.create_job(client, topic="Dedup")
        _run_to_video(client, monkeypatch, job_id)

        gate.arm()
        try:
            first = client.post(f"/api/jobs/{job_id}/video/regenerate",
                                json={"actor": "harness"})
            assert first.status_code == 200, first.text
            gate.wait_started()
            second = client.post(f"/api/jobs/{job_id}/video/regenerate",
                                 json={"actor": "harness"})
            assert second.status_code == 409, second.text
            job = harness.get_job(client, job_id)
            assert job["active_video_version"] == 1
            assert len(job["video_versions"]) == 1, (
                "a refused duplicate must not register a version")
        finally:
            gate.open()
        harness.drain_executor()

        job = harness.wait_for_video(client, job_id, min_version=2)
        assert job["active_video_version"] == 2
        assert [path.name for path in harness.version_paths(job_id)] == [
            "video-v1.mp4", "video-v2.mp4"]
        harness.record_row("dedup", 409, second.status_code, job_id=job_id,
                           refused_status=second.status_code,
                           versions=job["active_video_version"])

    def test_cancel_during_render_discards_the_result(self, client, monkeypatch, tmp_path):
        """Cancelling a held render leaves the approved version untouched."""
        gate = _RenderGate(client, monkeypatch, tmp_path)
        job_id = harness.create_job(client, topic="Cancel in flight")
        _run_to_video(client, monkeypatch, job_id)
        approved = harness.get_job(client, job_id)
        first_sha = approved["video_versions"][0]["sha256"]

        gate.arm()
        try:
            assert client.post(f"/api/jobs/{job_id}/video/regenerate",
                               json={"actor": "harness"}).status_code == 200
            gate.wait_started()
            cancelled = client.post(f"/api/jobs/{job_id}/cancel", json={"actor": "harness"})
            assert cancelled.status_code == 200, cancelled.text
        finally:
            gate.open()
        harness.drain_executor()

        job = harness.get_job(client, job_id)
        assert job["status"] == "CANCELLED"
        assert job["active_video_version"] == 1
        assert len(job["video_versions"]) == 1, "a cancelled render must add no version"
        assert job["video_versions"][0]["sha256"] == first_sha
        assert [path.name for path in harness.version_paths(job_id)] == ["video-v1.mp4"]
        assert not (harness.final_dir(job_id) / "video-v2.mp4").exists()
        harness.record_row("cancel", "CANCELLED", job["status"], job_id=job_id,
                           status=job["status"], versions=len(job["video_versions"]))

    def test_late_voice_result_after_cancel_is_refused(self, fleet, monkeypatch):
        """A Worker that finishes after the cancel cannot write narration."""
        job_id = harness.create_job(fleet, topic="Stale result")
        harness.approve_script(fleet, job_id, monkeypatch)
        harness.wait_for_status(fleet, job_id, "ASSET_GENERATION", "TTS_GENERATING",
                                "VIDEO_GENERATION")
        for _ in range(2):
            harness.submit_image(fleet, job_id)
        harness.wait_for_status(fleet, job_id, "TTS_GENERATING", "VIDEO_GENERATION")

        task = harness.claim_task(fleet, job_id, harness.VOICE_NODE, task="voice",
                                  supported_tasks=["voice"],
                                  capabilities=["speech_synthesis"],
                                  voice_catalog={"voices": ["uk"],
                                                 "models": ["uk_male"]})
        assert fleet.post(f"/api/jobs/{job_id}/cancel",
                          json={"actor": "harness"}).status_code == 200

        from worker import role_executor
        artifact = role_executor._artifact("narration.wav", "tts", harness.make_wav())
        response = fleet.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": task["task_id"], "node_name": harness.VOICE_NODE,
            "success": True, "artifacts": [artifact]})
        assert response.status_code == 409, response.text

        job = harness.get_job(fleet, job_id)
        assert job["status"] == "CANCELLED"
        assert all(not scene.get("narration") for scene in job["scenes"]), (
            "the late narration must not be attached to the cancelled Job")
        assert job["video_versions"] == []
        harness.record_row("stale_result", 409, response.status_code, job_id=job_id,
                           refused_status=response.status_code,
                           detail=response.json().get("detail"))

    def test_expired_lease_requeues_and_fences_the_stale_result(self, fleet, monkeypatch):
        """The watchdog requeues an expired lease; the old holder loses the claim."""
        from core.app import handle_expired_storyboard_lease

        job_id = harness.create_job(fleet, topic="Lease expiry")
        harness.wait_for_status(fleet, job_id, "SCRIPT_QUEUED")
        harness.run_script_task(fleet, job_id)
        harness.wait_for_status(fleet, job_id, "SCRIPT_PENDING_APPROVAL")
        assert fleet.post(f"/api/jobs/{job_id}/script/approve",
                          json={"actor": "harness"}).status_code == 200
        harness.wait_for_status(fleet, job_id, "STORYBOARD_QUEUED")

        first = harness.claim_task(fleet, job_id, harness.TEXT_NODE, task="storyboard")

        # the lease expires: the real watchdog path requeues it within budget
        expired = task_queue.requeue_expired(now=time.time() + 3600)
        requeued = [task for task in expired if task.get("job_id") == job_id]
        assert len(requeued) == 1, expired
        assert handle_expired_storyboard_lease(store, requeued[0]) is True
        job = harness.get_job(fleet, job_id)
        assert job["status"] == "STORYBOARD_QUEUED"
        assert any("LEASE EXPIRED; REQUEUED" in line for line in job["events"])
        replacement = store.jobs[job_id].storyboard_task_id
        assert replacement and replacement != first["task_id"]

        # the previous holder's late result is fenced: no storyboard, no version
        board = _storyboard_artifact(job_id)
        late = fleet.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": first["task_id"], "node_name": harness.TEXT_NODE,
            "success": True, "artifacts": [board]})
        assert late.status_code == 409, late.text
        assert store.jobs[job_id].storyboards == [], (
            "a result from an expired lease must not register a storyboard")
        assert store.jobs[job_id].storyboard_task_id == replacement, (
            "the requeued task must still await its legitimate holder")

        # a second Text Worker takes the requeued task and finishes the storyboard
        from worker import role_executor

        second_node = f"{harness.TEXT_NODE}-b"
        harness.heartbeat(fleet, second_node, role="text", vram_mb=0,
                          supported_tasks=["text"], capabilities=["text_generation"])
        task = harness.claim_task(fleet, job_id, second_node, task="storyboard")
        assert task["task_id"] == replacement
        payload = harness._storyboard_llm_stub(job_id)
        monkeypatch.setattr(
            role_executor.httpx, "post",
            lambda *a, **kw: harness.StubHTTPResponse(
                {"response": json.dumps(payload, ensure_ascii=False)}))
        [artifact] = role_executor.execute_role_task("text", task)
        assert fleet.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": task["task_id"], "node_name": second_node,
            "success": True, "artifacts": [artifact]}).status_code == 200

        job = harness.wait_for_status(fleet, job_id, "STORYBOARD_PENDING_APPROVAL")
        assert len(job["storyboards"]) == 1
        assert job["storyboards"][0]["version"] == 1
        harness.record_row("lease", 409, late.status_code, job_id=job_id,
                           expired_task=first["task_id"], requeued_task=replacement,
                           stale_result_status=late.status_code)


class TestRestartRecovery:
    def test_restart_releases_a_stale_claim_and_keeps_the_approval(self, client,
                                                                   monkeypatch, tmp_path):
        """A CORE restart must not strand an approval nor keep a dead render claim."""
        gate = _RenderGate(client, monkeypatch, tmp_path)
        job_id = harness.create_job(client, topic="Restart recovery")
        _run_to_video(client, monkeypatch, job_id)
        first_sha = harness.get_job(client, job_id)["video_versions"][0]["sha256"]

        # a regeneration is in flight when CORE goes down
        gate.arm()
        try:
            assert client.post(f"/api/jobs/{job_id}/video/regenerate",
                               json={"actor": "harness"}).status_code == 200
            gate.wait_started()
            assert store.jobs[job_id].video_regenerating is True

            restarted = harness.restart_store()
            replayed = restarted.jobs[job_id]
            assert replayed.active_video_version == 1
            assert [version.approved for version in replayed.video_versions] == [False]
            assert replayed.video_versions[0].sha256 == first_sha
            assert replayed.video_regenerating is False, (
                "a persisted regeneration claim cannot survive a restart")
            assert any("STALE VIDEO REGENERATION CLAIM RELEASED" in line
                       for line in replayed.events)
        finally:
            gate.open()
        harness.drain_executor()

        # The in-process harness cannot kill the executor thread the way a real
        # CORE restart would, so the abandoned render may still finish; what must
        # hold is version integrity: v1 is untouched and the next attempt is v2.
        job = harness.wait_for_status(client, job_id, "VIDEO_PENDING_APPROVAL")
        assert job["video_versions"][0]["sha256"] == first_sha
        assert [path.name for path in harness.version_paths(job_id)] in (
            ["video-v1.mp4"], ["video-v1.mp4", "video-v2.mp4"])
        assert store.jobs[job_id].video_regenerating is False

        # an approved version survives another restart
        job = harness.approve_video(client, job_id,
                                    version=job["active_video_version"])
        assert job["status"] == "READY"
        approved = harness.restart_store().jobs[job_id]
        assert approved.status == JobStatus.READY
        assert approved.active_video_version == job["active_video_version"]
        assert approved.video_versions[-1].approved is True
        assert approved.video_versions[0].approved is False
        assert len({version.sha256 for version in approved.video_versions}) == \
            len(approved.video_versions), "every version keeps its own artifact"
        harness.record_row("restart", True, approved.video_versions[-1].approved,
                           job_id=job_id, stale_claim_released=True,
                           approved_after_restart=True,
                           video=harness.video_evidence(job_id, job))


class TestPartialPublish:
    def test_one_channel_delivers_while_another_retries_then_fails(self, fleet, monkeypatch):
        """A partial publish keeps the delivered receipt and bounds the retry."""
        job_id = harness.create_job(fleet, topic="Partial publish")
        _run_to_video(fleet, monkeypatch, job_id)
        job = harness.approve_video(fleet, job_id, version=1)
        approved_sha = job["video_versions"][0]["sha256"]

        harness.register_publisher(fleet)
        response = fleet.post(f"/api/jobs/{job_id}/publish", json=["youtube", "telegram"])
        assert response.status_code == 200, response.text

        # a Publisher Worker holds one task at a time, so the channels are
        # delivered one after the other
        youtube = harness.claim_publish(fleet, job_id, channel="youtube")
        assert youtube["publish_intent"]["video_version"] == 1
        assert youtube["publish_intent"]["video_sha256"] == approved_sha
        harness.submit_publish_receipt(fleet, job_id, youtube,
                                       harness.sandbox_receipt("youtube", youtube))

        telegram = harness.claim_publish(fleet, job_id, channel="telegram")
        harness.submit_publish_receipt(
            fleet, job_id, telegram,
            harness.sandbox_receipt("telegram", telegram, status="FAILED",
                                    error="telegram 503"))

        # the failed channel is retried once inside the budget, not endlessly
        job = harness.get_job(fleet, job_id)
        assert job["publication_results"]["youtube"]["status"] == "PUBLISHED"
        assert job["publication_results"]["youtube"]["remote_id"] == "mock-youtube"
        assert job["publication_results"]["youtube"]["video_sha256"] == approved_sha
        assert "youtube" in job["published_to"]
        assert any("PUBLISH telegram: RETRY 1/" in line for line in job["events"])
        retry = harness.claim_publish(fleet, job_id, channel="telegram")
        assert retry["task_id"] != telegram["task_id"], "the retry is a new attempt"

        # receipt correlation: a receipt for another video version cannot satisfy the task
        forged = harness.sandbox_receipt("telegram", retry)
        forged["video_version"] = 99
        harness.submit_publish_receipt(fleet, job_id, retry, forged)
        job = harness.get_job(fleet, job_id)
        assert any("PUBLISH telegram FAILED: RECEIPT REJECTED" in line
                   for line in job["events"]), job["events"][-3:]
        assert job["status"] == "FAILED"
        assert job["published_to"] == ["youtube"], (
            "the delivered channel must survive the failed one")
        assert job["publication_results"]["youtube"]["remote_id"] == "mock-youtube"
        harness.record_row("partial_publish", ["youtube"], job["published_to"],
                           job_id=job_id, delivered=job["published_to"],
                           results=job["publication_results"],
                           retry_budget=job.get("max_retries"))


class TestBackupAndUpdate:
    def test_backup_snapshot_and_update_gate_keep_immutable_versions(self, fleet, monkeypatch,
                                                                     tmp_path):
        """A real encrypted snapshot covers the rendered versions; the update gate holds."""
        from services import backup_service

        job_id = harness.create_job(fleet, topic="Backup and update")
        _run_to_video(fleet, monkeypatch, job_id)
        job = harness.approve_video(fleet, job_id, version=1)
        sha = job["video_versions"][0]["sha256"]

        root = tmp_path / "mounted-backups"
        key_file = tmp_path / "backup_encryption_key"
        key_file.write_text("cd" * 32, encoding="ascii")
        monkeypatch.setenv("BACKUP_ROOT", str(root))
        monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(key_file))
        monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("BACKUP_SOURCES", f"storage:{harness.job_root(job_id)}")
        monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")
        monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
        monkeypatch.delenv("BACKUP_REMOTE_CMD", raising=False)

        with TestClient(backup_service.app) as backup:
            assert backup.get("/health").json()["encryption"] == "AES-256-GCM"
            created = backup.post("/snapshots", json={"job_id": job_id})
            assert created.status_code == 200, created.text
            receipt = created.json()
        assert receipt["job_id"] == job_id
        blob = (root / receipt["file"]).read_bytes()
        assert blob.startswith(backup_service.MAGIC)
        assert sha.encode("ascii") not in blob, "the snapshot must be encrypted"
        assert (root / f"{receipt['snapshot_id']}.json").is_file()

        # the receipt survives a restart of the backup service
        with TestClient(backup_service.app) as backup:
            assert backup.get("/snapshots").json()["snapshots"] == [receipt]

        # the update gate: this install only ever accepts a forward release
        from core.update_protocol import validate_replay_state

        trusted = {"release_sequence": 15, "version": "1.5.0", "sha256": "a" * 64,
                   "key_id": "release-2026"}
        current = {"version": Path("VERSION").read_text(encoding="utf-8").strip(),
                   "sha256": sha, "signature": "local-install"}
        forward = {**current, "release_sequence": 16, "version": "9.9.9"}
        accepted = validate_replay_state(forward, trusted)
        assert accepted["release_sequence"] == 16
        assert accepted["version"] == "9.9.9"
        replayed = {**current, "release_sequence": 15, "version": "8.8.8"}
        with pytest.raises(RuntimeError, match="replayed|equivocates"):
            validate_replay_state(replayed, trusted)

        # the snapshot still holds the approved artifact after the update check
        assert (harness.final_dir(job_id) / "video-v1.mp4").is_file()
        harness.record_row("backup_update", True,
                           blob.startswith(backup_service.MAGIC)
                           and sha.encode("ascii") not in blob,
                           job_id=job_id, snapshot=receipt["snapshot_id"],
                           encrypted=True, receipt_durable=True, video_sha256=sha)


class TestEvidence:
    def test_every_row_records_sha_anchored_evidence(self, fleet, monkeypatch, tmp_path):
        """CI evidence is produced from the same run: SHA-anchored artifacts + receipts."""
        job_id = harness.create_job(fleet, topic="Evidence run")
        _run_to_video(fleet, monkeypatch, job_id)
        job = harness.approve_video(fleet, job_id, version=1)
        harness.register_publisher(fleet)
        assert fleet.post(f"/api/jobs/{job_id}/publish",
                          json=["youtube"]).status_code == 200
        task = harness.claim_publish(fleet, job_id, channel="youtube")
        harness.submit_publish_receipt(fleet, job_id, task,
                                       harness.sandbox_receipt("youtube", task))

        job = harness.get_job(fleet, job_id)
        evidence = harness.video_evidence(job_id, job)
        files = harness.version_paths(job_id)
        on_disk = hashlib.sha256(files[0].read_bytes()).hexdigest()
        assert evidence["versions"][0]["sha256"] == on_disk, (
            "the recorded version must be anchored to the artifact on disk")
        assert evidence["files"][str(files[0])] == on_disk
        assert job["publication_results"]["youtube"]["video_sha256"] == on_disk

        harness.record_row("evidence", on_disk,
                           job["publication_results"]["youtube"]["video_sha256"],
                           job_id=job_id, video=evidence,
                           receipts=job["publication_results"],
                           artifacts_on_disk=[str(p) for p in files],
                           provenance=harness.run_provenance())
        target = tmp_path / harness.EVIDENCE_ENV.lower() / "evidence.json"
        monkeypatch.setenv(harness.EVIDENCE_ENV, str(target))
        harness.record_evidence("evidence", {"video": evidence})
        document = json.loads(target.read_text(encoding="utf-8"))
        assert document["evidence"]["video"]["versions"][0]["sha256"] == on_disk
        # every recorded row carries the SHA and the configuration it ran with
        provenance = document["evidence"]["provenance"]
        assert provenance["version"] == Path("VERSION").read_text(encoding="utf-8").strip()
        assert provenance["sha"] and len(provenance["sha"]) == 40
        assert provenance["config"]["backends"]["llm"]["backend"] == "ollama"
        assert provenance["config"]["backends"]["compute"]["backend"] == "vertep-worker"
        harness.record_evidence("evidence", {
            "job_id": job_id,
            "video_sha256": on_disk,
            "receipt": job["publication_results"]["youtube"],
        })


def _storyboard_artifact(job_id: str) -> dict:
    """A storyboard artifact a stale Worker could still hand in."""
    from worker import role_executor
    payload = harness._storyboard_llm_stub(job_id)
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return role_executor._artifact("storyboard.json", "storyboard", encoded)
