import json
import pytest

from core.models import JobStatus, StoryboardScene, StoryboardVersion
from core.pipeline import JobStore
from core.storyboard import StoryboardConflict, StoryboardService
from core.storyboard_telegram import render_storyboard, storyboard_keyboard


class FakeClient:
    model = "fake-storyboard"

    def __init__(self, responses):
        self.responses = responses

    def complete(self, prompt, *, format_json=False):
        assert format_json is True
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def storyboard_payload(title="Тестова історія"):
    return json.dumps({
        "title": title,
        "description": "Опис",
        "hashtags": ["#vertep"],
        "scenes": [
            {"prompt": "Українське місто", "video_prompt": "Повільна панорама",
             "voiceover": "Початок історії", "duration": 6},
            {"prompt": "Герой у кадрі", "video_prompt": "Камера наближається",
             "voiceover": "Продовження", "duration": 7},
        ],
    }, ensure_ascii=False)


def _storyboard_artifact(version: int, title: str = "Тестова історія"):
    import base64
    return {
        "filename": "storyboard.json",
        "kind": "storyboard",
        "data_base64": base64.b64encode(json.dumps({
            "version": version,
            "title": title,
            "description": "Опис",
            "hashtags": ["#vertep"],
            "scenes": [
                {"index": 1, "prompt": "Українське місто", "video_prompt": "Повільна панорама",
                 "voiceover": "Початок історії", "duration": 6},
                {"index": 2, "prompt": "Герой у кадрі", "video_prompt": "Камера наближається",
                 "voiceover": "Продовження", "duration": 7},
            ],
            "prompt_version": "1.0",
            "model": "fake-storyboard",
            "status": "pending_approval",
            "revision_request": None,
            "image_version": 1,
            "image_status": "pending",
        }, ensure_ascii=False).encode("utf-8")).decode("ascii"),
    }


@pytest.fixture
def job_store(tmp_path, monkeypatch):
    character = tmp_path / "characters" / "hero"
    character.mkdir(parents=True)
    (character / "character.json").write_text(
        json.dumps({"id": "hero", "name": "Герой"}, ensure_ascii=False), encoding="utf-8"
    )
    (character / "system_prompt.txt").write_text("Говори українською", encoding="utf-8")
    brand = tmp_path / "brands" / "brand01"
    brand.mkdir(parents=True)
    (brand / "brand.json").write_text(
        json.dumps({"id": "brand01", "name": "Бренд"}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path / "characters"))
    monkeypatch.setenv("BRANDS_ROOT", str(tmp_path / "brands"))
    monkeypatch.setenv("STORYBOARD_TARGET_DURATION", "13")
    return JobStore(str(tmp_path / "jobs"))


def test_storyboard_version_approval_and_stale_version_guard(job_store):
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)

    service.queue(job)
    task_id = job.storyboard_task_id
    assert task_id is not None
    assert job.status == JobStatus.STORYBOARD_QUEUED

    # Simulate worker completing the task
    service.handle_result(job.job_id, task_id, True, [_storyboard_artifact(1)], None)

    assert job.status == JobStatus.STORYBOARD_PENDING_APPROVAL
    first = job.storyboards[0]
    assert first.version == 1

    # Issue #6: storyboard cannot be approved before image previews are approved
    with pytest.raises(StoryboardConflict):
        service.approve(job.job_id, 1, "tester")

    # Simulate GPU worker completing image previews and approving them
    for scene in first.scenes:
        scene.image_artifact_id = f"artifact-{scene.index}"
    first.image_status = "ready"
    service.approve_images(job.job_id, 1, "tester")
    assert first.image_status == "approved"

    service.regenerate(job.job_id, 1, "tester", "Зроби динамічніше")
    # In async flow, active_storyboard_version stays at 1 until new storyboard is generated
    assert job.active_storyboard_version == 1
    assert first.status == "superseded"
    with pytest.raises(StoryboardConflict):
        service.approve(job.job_id, 1, "tester")

    # Simulate second storyboard generation (regenerate already called queue)
    task_id2 = job.storyboard_task_id
    service.handle_result(job.job_id, task_id2, True, [_storyboard_artifact(2, "Друга версія")], None)

    # Now active version should be 2
    assert job.active_storyboard_version == 2
    second = next(s for s in job.storyboards if s.version == 2)
    with pytest.raises(StoryboardConflict):
        service.approve(job.job_id, 2, "tester")
    for scene in second.scenes:
        scene.image_artifact_id = f"artifact2-{scene.index}"
    second.image_status = "ready"
    service.approve_images(job.job_id, 2, "tester")
    service.approve(job.job_id, 2, "tester")
    assert job.status == JobStatus.STORYBOARD_APPROVED
    assert job.script["scenes"][0]["prompt"] == "Українське місто"
    assert job.storyboards[-1].decided_by == "tester"


def test_storyboard_retries_and_fails(job_store, monkeypatch):
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)

    service.queue(job)
    task_id = job.storyboard_task_id

    # First attempt fails
    service.handle_result(job.job_id, task_id, False, None, "bad json")
    assert job.storyboard_attempt == 1
    assert job.status == JobStatus.STORYBOARD_QUEUED  # Re-queued for retry

    # Second attempt fails
    task_id2 = job.storyboard_task_id
    service.handle_result(job.job_id, task_id2, False, None, "bad json again")
    assert job.storyboard_attempt == 2

    # Third attempt fails
    task_id3 = job.storyboard_task_id
    with pytest.raises(RuntimeError):
        service.handle_result(job.job_id, task_id3, False, None, "bad json third")
    assert job.storyboard_attempt == 3
    assert job.status == JobStatus.STORYBOARD_FAILED
    assert job.storyboard_error


def test_telegram_storyboard_rendering_and_versioned_callbacks(job_store):
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)
    service.queue(job)
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, True, [_storyboard_artifact(1)], None)

    storyboard = job.storyboards[0]

    chunks = render_storyboard(job, storyboard, limit=180)
    assert len(chunks) > 1
    assert all(len(chunk) <= 180 for chunk in chunks)
    callbacks = storyboard_keyboard(job.job_id, storyboard.version)
    callback_data = [button["callback_data"] for button in callbacks["inline_keyboard"][0]]
    assert f"sb_ok:{job.job_id}:1" in callback_data
    assert f"sb_regen:{job.job_id}:1" in callback_data
    assert f"sb_edit:{job.job_id}:1" in callback_data
    assert f"sb_reject:{job.job_id}:1" in callback_data


def test_video_blocked_until_image_storyboard_approved(job_store):
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)
    service.queue(job)
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, True, [_storyboard_artifact(1)], None)

    sb = job.storyboards[0]
    # Before image approval — storyboard approve must be blocked (Issue #6)
    with pytest.raises(StoryboardConflict):
        service.approve(job.job_id, sb.version, "tester")
    # Even with artifacts placeholder, image_status is still pending — still blocked
    with pytest.raises(StoryboardConflict):
        service.approve(job.job_id, sb.version, "tester")


def test_image_revision_preserves_old_artifacts(job_store):
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)
    service.queue(job)
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, True, [_storyboard_artifact(1)], None)

    first = job.storyboards[0]
    for scene in first.scenes:
        scene.image_artifact_id = f"artifact-{scene.index}"
    first.image_status = "ready"
    service.approve_images(job.job_id, 1, "tester")
    first_artifact = first.scenes[0].image_artifact_id

    service.regenerate(job.job_id, 1, "tester", "Зміни")
    # Simulate second storyboard generation
    task_id2 = job.storyboard_task_id
    service.handle_result(job.job_id, task_id2, True, [_storyboard_artifact(2)], None)

    second = next(s for s in job.storyboards if s.version == 2)
    # Old artifact stays on superseded version
    assert first.scenes[0].image_artifact_id == first_artifact
    assert first.status == "superseded"
    assert second.status == "pending_approval"


def test_stale_image_approval_rejected_by_reviewed_version(job_store):
    """Issue #36/#6: a stale image approval/revision must not act on a newer image_version.

    The client transmits the image_version it actually reviewed; approving with a value
    that no longer matches the current image_version is a conflict, not a silent success.
    """
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)
    service.queue(job)
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, True, [_storyboard_artifact(1)], None)
    sb = job.storyboards[0]
    for scene in sb.scenes:
        scene.image_artifact_id = f"artifact-{scene.index}"
    sb.image_status = "ready"
    assert sb.image_version == 1

    # Reviewing v1 when the current image version is v2 is stale -> rejected.
    with pytest.raises(StoryboardConflict):
        service.approve_images(job.job_id, sb.version, "tester", expected_image_version=2)
    with pytest.raises(StoryboardConflict):
        service.request_image_revision(job.job_id, sb.version, "tester", [1], "x",
                                       expected_image_version=2)
    # The storyboard was not silently approved by the stale request.
    assert sb.image_status == "ready"

    # Matching reviewed version succeeds.
    service.approve_images(job.job_id, sb.version, "tester", expected_image_version=1)
    assert sb.image_status == "approved"


def test_image_storyboard_gate_blocks_unapproved_video_assets(job_store, monkeypatch):
    """Issue #36/#6: Resume/Retry (job reset to NEW with a ready script) must not start
    video/image assets while the ACTIVE image storyboard is not approved.
    """
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    from core.api.job_helpers import _image_storyboard_gate

    job = job_store.create("Нова тема", "hero", 5)
    job.script = {"title": "Тема", "scenes": [{"prompt": "p", "duration": 1}]}
    scenes = [StoryboardScene(index=i, prompt=f"p{i}", video_prompt=f"v{i}",
                              voiceover="", duration=1, image_prompt=f"p{i}",
                              image_artifact_id=f"art-{i}") for i in (1, 2)]
    sb = StoryboardVersion(version=1, title="Тема", description="", hashtags=[],
                           scenes=scenes, status="pending_approval",
                           image_status="ready", image_version=1)
    job.storyboards = [sb]
    job.active_storyboard_version = 1

    # Preview images exist but were never approved -> gate blocks asset generation.
    assert _image_storyboard_gate(job_store, job) is True

    # Once the active image storyboard is approved, the gate opens.
    sb.image_status = "approved"
    assert _image_storyboard_gate(job_store, job) is False


def test_image_storyboard_gate_blocks_missing_or_mismatched_storyboard(job_store, monkeypatch):
    """Issue #60/#36 negative acceptance: video tasks must never start video asset
    generation without an active storyboard or when active_storyboard_version is mismatched.
    """
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    from core.api.job_helpers import _image_storyboard_gate, _prepare_and_dispatch

    video_job = job_store.create("Відео тема", "hero", 5, task_type="video")
    video_job.script = {"title": "Тема", "scenes": [{"prompt": "p", "duration": 1}]}
    video_job.storyboards = []
    video_job.active_storyboard_version = None

    # Case 1: Missing storyboard completely blocks video asset generation
    assert _image_storyboard_gate(job_store, video_job) is True

    # Dispatch must not transition video_job to ASSET_GENERATION
    _prepare_and_dispatch(video_job)
    assert video_job.status == JobStatus.NEW

    # Case 2: Mismatched active version (active=2, only v1 present)
    sc = StoryboardScene(index=1, prompt="p", video_prompt="v", voiceover="", duration=1)
    sb1 = StoryboardVersion(version=1, title="Тема", description="", hashtags=[],
                            scenes=[sc], status="approved", image_status="approved", image_version=1)
    video_job.storyboards = [sb1]
    video_job.active_storyboard_version = 2
    assert _image_storyboard_gate(job_store, video_job) is True

    _prepare_and_dispatch(video_job)
    assert video_job.status == JobStatus.NEW


def test_storyboard_rest_contract_is_registered():
    from core.app import app

    paths = app.openapi()["paths"]
    assert "/api/jobs/{job_id}/storyboards" in paths
    assert "/api/jobs/{job_id}/storyboards/{version}" in paths
    assert "/api/jobs/{job_id}/storyboards/generate" in paths
    assert "/api/jobs/{job_id}/storyboards/approve" in paths
    assert "/api/jobs/{job_id}/storyboards/reject" in paths
    assert "/api/jobs/{job_id}/storyboards/regenerate" in paths
    # Issue #6 image storyboard endpoints
    assert "/api/jobs/{job_id}/storyboards/images/approve" in paths
    assert "/api/jobs/{job_id}/storyboards/images/revision" in paths
    assert "/api/jobs/{job_id}/storyboards/images/regenerate" in paths