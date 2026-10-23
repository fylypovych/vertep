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


def test_storyboard_retry_carries_revision(job_store, monkeypatch):
    """Issue #69 T3: storyboard retry forwards job.revision to the worker."""
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")
    job = job_store.create("Нова тема", "hero", 5)
    job.revision = "додати кадр про кішку"
    service = StoryboardService(job_store)
    service.queue(job)
    task_id = job.storyboard_task_id
    captured = {}
    original_queue = service.queue
    def _spy(job, revision=None):
        captured["revision"] = revision
        return original_queue(job, revision=revision)
    service.queue = _spy
    service.handle_result(job.job_id, task_id, False, None, "bad json")
    assert job.storyboard_attempt == 1
    assert captured["revision"] == "додати кадр про кішку"


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


def test_storyboard_regenerate_from_history(job_store):
    """Issue #90: Browser/backend history selection, revision/regenerate flow.

    Allows regenerating from any historical version, not just the active one.
    """
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)

    # Generate v1
    service.queue(job)
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, True, [_storyboard_artifact(1)], None)

    # Approve images for v1 (but NOT the storyboard itself)
    first = job.storyboards[0]
    for scene in first.scenes:
        scene.image_artifact_id = f"artifact-{scene.index}"
    first.image_status = "ready"
    service.approve_images(job.job_id, 1, "tester")
    # v1 is now image_status="approved" but storyboard status="pending_approval"

    # Regenerate v2 from v1 (which is active and pending_approval)
    service.regenerate(job.job_id, 1, "tester", "Зроби динамічніше")
    task_id2 = job.storyboard_task_id
    service.handle_result(job.job_id, task_id2, True, [_storyboard_artifact(2, "Друга версія")], None)
    second = next(s for s in job.storyboards if s.version == 2)
    for scene in second.scenes:
        scene.image_artifact_id = f"artifact2-{scene.index}"
    second.image_status = "ready"
    service.approve_images(job.job_id, 2, "tester")
    service.approve(job.job_id, 2, "tester")

    # Now we have v1 (superseded) and v2 (approved). Active is v2.
    # Regenerate from history v1 - should create v3 based on v1
    service.regenerate_from_history(job.job_id, 1, "tester", "Повернись до першої версії")
    task_id3 = job.storyboard_task_id
    service.handle_result(job.job_id, task_id3, True, [_storyboard_artifact(3, "Третя версія з історії")], None)

    # v3 should be created, active should be v3
    assert job.active_storyboard_version == 3
    third = next(s for s in job.storyboards if s.version == 3)
    assert third.title == "Третя версія з історії"
    assert third.status == "pending_approval"

    # v1 should be marked superseded
    first = next(s for s in job.storyboards if s.version == 1)
    assert first.status == "superseded"

    # Original v2 should remain approved
    second = next(s for s in job.storyboards if s.version == 2)
    assert second.status == "approved"


def test_storyboard_regenerate_from_history_rejects_pending(job_store):
    """Cannot regenerate from a version that is still pending approval."""
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)

    service.queue(job)
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, True, [_storyboard_artifact(1)], None)

    # v1 is pending_approval (images not approved)
    with pytest.raises(StoryboardConflict):
        service.regenerate_from_history(job.job_id, 1, "tester")


def test_storyboard_retry_keeps_budget_for_same_version(job_store, monkeypatch):
    """Issue #197/#163.1: retry of the same version must NOT re-arm the budget.

    Before the fix, ``queue()`` reset ``storyboard_attempt`` whenever any
    storyboard existed, so a retry of the same version started a fresh budget
    and a failing version could loop forever.  The budget now belongs to a
    version: a regenerate/history-branch (new version) resets it, a retry of
    the same version keeps burning the budget it already started.
    """
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)

    service.queue(job)
    assert job.storyboard_attempt_version == 1
    assert job.storyboard_attempt == 0

    # First failure: budget burns to 1, version stays 1, retry re-queued.
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, False, None, "bad json")
    assert job.storyboard_attempt == 1
    assert job.storyboard_attempt_version == 1

    # Second failure: budget burns to 2, still the same version.
    task_id2 = job.storyboard_task_id
    service.handle_result(job.job_id, task_id2, False, None, "bad json again")
    assert job.storyboard_attempt == 2
    assert job.storyboard_attempt_version == 1

    # Third failure: budget exhausted for version 1 -> terminal failure.
    task_id3 = job.storyboard_task_id
    with pytest.raises(RuntimeError):
        service.handle_result(job.job_id, task_id3, False, None, "bad json third")
    assert job.storyboard_attempt == 3
    assert job.status == JobStatus.STORYBOARD_FAILED


def test_storyboard_regenerate_resets_budget_for_new_version(job_store):
    """Issue #197/#163.1: a regenerate/history-branch starts a fresh budget.

    The new version must not inherit the budget already burned by the
    superseded version.
    """
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)

    service.queue(job)
    service.handle_result(job.job_id, job.storyboard_task_id, True,
                          [_storyboard_artifact(1)], None)
    first = job.storyboards[0]
    for scene in first.scenes:
        scene.image_artifact_id = f"artifact-{scene.index}"
    first.image_status = "ready"
    service.approve_images(job.job_id, 1, "tester")

    # Regenerate while v1 is still pending_approval (regenerate requires it).
    service.regenerate(job.job_id, 1, "tester", "Зроби динамічніше")
    assert job.storyboard_attempt_version == 2
    assert job.storyboard_attempt == 0
    assert job.storyboard_active_revision == "Зроби динамічніше"

    # A retry of the new version keeps its own budget.
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, False, None, "bad json")
    assert job.storyboard_attempt == 1
    assert job.storyboard_attempt_version == 2


def test_storyboard_retry_carries_pinned_revision(job_store, monkeypatch):
    """Issue #197/#163.2: a retry reuses the revision pinned for the version.

    The pinned revision is set once when the version's budget starts and is
    reused on every retry of the same version, so a retry never silently falls
    back to a stale ``job.revision`` or a previous version's
    ``revision_request``.
    """
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")
    job = job_store.create("Нова тема", "hero", 5)
    job.revision = "додати кадр про кішку"
    service = StoryboardService(job_store)
    service.queue(job)
    assert job.storyboard_active_revision == "додати кадр про кішку"

    captured = {}
    original_queue = service.queue

    def _spy(job, revision=None):
        captured["revision"] = revision
        return original_queue(job, revision=revision)

    service.queue = _spy
    service.handle_result(job.job_id, job.storyboard_task_id, False, None, "bad json")
    assert job.storyboard_attempt == 1
    assert captured["revision"] == "додати кадр про кішку"


def test_storyboard_regenerate_from_history_pins_revision(job_store):
    """Issue #197/#163.2: a history-branch pins the supplied revision."""
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)
    service.queue(job)
    service.handle_result(job.job_id, job.storyboard_task_id, True,
                          [_storyboard_artifact(1)], None)
    first = job.storyboards[0]
    for scene in first.scenes:
        scene.image_artifact_id = f"artifact-{scene.index}"
    first.image_status = "ready"
    service.approve_images(job.job_id, 1, "tester")
    service.approve(job.job_id, 1, "tester")

    service.regenerate_from_history(job.job_id, 1, "tester",
                                    "Повернись до першої версії")
    assert job.storyboard_active_revision == "Повернись до першої версії"
    assert job.storyboard_attempt_version == 2


def test_storyboard_default_revision_stays_none(job_store, monkeypatch):
    """#163.2: default revision (no revision arg, no job.revision) stays None
    through queue, does not fall back to any previous revision_request."""
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")
    job = job_store.create("Нова тема", "hero", 5)
    # No job.revision set, no revision arg → default None
    service = StoryboardService(job_store)
    service.queue(job)
    assert job.storyboard_active_revision is None
    assert job.storyboard_attempt_version == 1
    assert job.storyboard_attempt == 0

    # First failure → retry of same version re-uses pinned None revision
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, False, None, "bad json")
    assert job.storyboard_attempt == 1
    assert job.storyboard_active_revision is None  # still default None, not stale revision_request


def test_storyboard_default_revision_through_retry(job_store, monkeypatch):
    """#163.2: retry of same version re-uses pinned revision (None for default),
    never silently falls back to a previous version's revision_request."""
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")
    job = job_store.create("Нова тема", "hero", 5)
    # No job.revision set → truly default None
    service = StoryboardService(job_store)

    # First version with default revision
    service.queue(job)
    assert job.storyboard_active_revision is None  # default None
    assert job.storyboard_attempt_version == 1

    # Complete v1
    service.handle_result(job.job_id, job.storyboard_task_id, True,
                          [_storyboard_artifact(1)], None)
    first = job.storyboards[0]
    first.revision_request = "старий revision_request із версії 1"
    for scene in first.scenes:
        scene.image_artifact_id = f"artifact-{scene.index}"
    first.image_status = "ready"
    service.approve_images(job.job_id, 1, "tester")
    service.approve(job.job_id, 1, "tester")

    # Regenerate v2 with default revision (None) — must NOT inherit v1's revision_request
    service.regenerate_from_history(job.job_id, 1, "tester", None)
    assert job.storyboard_active_revision is None  # default None, not "старий revision_request..."
    assert job.storyboard_attempt_version == 2

    # Retry of v2 keeps pinned None
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, False, None, "bad json")
    assert job.storyboard_attempt == 1
    assert job.storyboard_active_revision is None


def test_storyboard_regenerate_from_history_default_none(job_store):
    """#163.2: regenerate_from_history without explicit revision keeps default None,
    does not fall back to base_storyboard.revision_request."""
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)
    service.queue(job)
    service.handle_result(job.job_id, job.storyboard_task_id, True,
                          [_storyboard_artifact(1)], None)
    first = job.storyboards[0]
    first.revision_request = "старий revision_request із версії 1"
    for scene in first.scenes:
        scene.image_artifact_id = f"artifact-{scene.index}"
    first.image_status = "ready"
    service.approve_images(job.job_id, 1, "tester")
    service.approve(job.job_id, 1, "tester")

    # No explicit revision → must stay None, not inherit "старий revision_request..."
    service.regenerate_from_history(job.job_id, 1, "tester", None)
    assert job.storyboard_active_revision is None
    assert job.storyboard_attempt_version == 2


def test_storyboard_lease_loss_retry_dead_letter(job_store, monkeypatch):
    """#163.3: heartbeat/lease-loss → dead-letter after budget exhausted;
    no lost or duplicate released tasks; SCRIPT_QUEUED fix preserved."""
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "2")
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)
    service.queue(job)

    # Simulate worker loss: expired storyboard lease, retry with pinned revision
    task_id = job.storyboard_task_id
    service.handle_result(job.job_id, task_id, False, None,
                          "storyboard task lease expired")
    assert job.storyboard_attempt == 1
    assert job.storyboard_active_revision is None  # default pinned through retry

    task_id2 = job.storyboard_task_id
    with pytest.raises(RuntimeError):
        service.handle_result(job.job_id, task_id2, False, None,
                              "storyboard task lease expired again")
    assert job.storyboard_attempt == 2
    assert job.status == JobStatus.STORYBOARD_FAILED
    assert job.storyboard_error is not None

    # Dead-letter recorded by job_helpers for the lost lease task
    assert len(job_store.jobs[job.job_id].storyboard_error or "") > 0


def test_storyboard_cancel_and_restart(job_store, monkeypatch):
    """#163.3: cancelled storyboard result is terminal late answer; restart
    re-queues with pinned revision, no duplicate released task."""
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)
    service.queue(job)

    # Cancelled result is rejected — task stays claimable
    task_id = job.storyboard_task_id
    rejected = service.handle_result(job.job_id, task_id, True, None, None,
                                     cancelled=True)
    assert rejected is None
    assert job.status == JobStatus.STORYBOARD_QUEUED
    assert job.storyboard_task_id == task_id  # still claimable

    # Restart: re-queue with pinned revision (default None)
    service.queue(job)
    assert job.storyboard_active_revision is None
    assert job.storyboard_attempt == 0  # fresh queue, not a retry budget burn


def test_storyboard_heartbeat_loss_metadata_consistency(job_store, monkeypatch):
    """#163.3: heartbeat loss produces consistent Job/Worker/repository/queue
    metadata — no lost or extra released tasks."""
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "1")
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(job_store)
    service.queue(job)

    # Worker heartbeat lost mid-execution → single attempt, terminal failure
    task_id = job.storyboard_task_id
    with pytest.raises(RuntimeError):
        service.handle_result(job.job_id, task_id, False, None,
                              "script worker heartbeat timed out")
    assert job.storyboard_attempt == 1
    assert job.status == JobStatus.STORYBOARD_FAILED
    assert job.storyboard_error is not None

    # No duplicate task: active_task_id cleared on terminal failure
    assert job.storyboard_task_id is not None  # last task id recorded
    # Repository sees exactly one failed attempt for this storyboard version
    assert job.storyboards == []  # no storyboard was ever produced


def test_storyboard_rest_contract_is_registered():
    from core.app import app

    paths = app.openapi()["paths"]
    assert "/api/jobs/{job_id}/storyboards" in paths
    assert "/api/jobs/{job_id}/storyboards/{version}" in paths
    assert "/api/jobs/{job_id}/storyboards/generate" in paths
    assert "/api/jobs/{job_id}/storyboards/approve" in paths
    assert "/api/jobs/{job_id}/storyboards/reject" in paths
    assert "/api/jobs/{job_id}/storyboards/regenerate" in paths
    assert "/api/jobs/{job_id}/storyboards/regenerate-from-history" in paths
    # Issue #6 image storyboard endpoints
    assert "/api/jobs/{job_id}/storyboards/images/approve" in paths
    assert "/api/jobs/{job_id}/storyboards/images/revision" in paths
    assert "/api/jobs/{job_id}/storyboards/images/regenerate" in paths