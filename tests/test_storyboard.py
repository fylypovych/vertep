import json

import pytest

from core.models import JobStatus
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
    responses = [storyboard_payload(), storyboard_payload("Друга версія")]
    service = StoryboardService(job_store, client_factory=lambda: FakeClient(responses))

    first = service.generate(job.job_id)
    assert job.status == JobStatus.STORYBOARD_PENDING_APPROVAL
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
    assert job.active_storyboard_version == 2
    assert first.status == "superseded"
    with pytest.raises(StoryboardConflict):
        service.approve(job.job_id, 1, "tester")

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
    responses = [ValueError("bad json"), "[]", json.dumps({"title": "Без сцен"})]
    service = StoryboardService(job_store, client_factory=lambda: FakeClient(responses))

    with pytest.raises(RuntimeError):
        service.generate(job.job_id)
    assert job.storyboard_attempt == 3
    assert job.status == JobStatus.STORYBOARD_FAILED
    assert job.storyboard_error


def test_telegram_storyboard_rendering_and_versioned_callbacks(job_store):
    job = job_store.create("Нова тема", "hero", 5)
    service = StoryboardService(
        job_store, client_factory=lambda: FakeClient([storyboard_payload()])
    )
    storyboard = service.generate(job.job_id)

    chunks = render_storyboard(job, storyboard, limit=180)
    assert len(chunks) > 1
    assert all(len(chunk) <= 180 for chunk in chunks)
    callbacks = storyboard_keyboard(job.job_id, storyboard.version)
    callback_data = [button["callback_data"] for button in callbacks["inline_keyboard"][0]]
    assert f"sb_ok:{job.job_id}:1" in callback_data
    assert f"sb_regen:{job.job_id}:1" in callback_data
    assert f"sb_edit:{job.job_id}:1" in callback_data
    assert f"sb_reject:{job.job_id}:1" in callback_data


def test_storyboard_rest_contract_is_registered():
    from core.app import app

    paths = app.openapi()["paths"]
    assert "/api/jobs/{job_id}/storyboards" in paths
    assert "/api/jobs/{job_id}/storyboards/{version}" in paths
    assert "/api/jobs/{job_id}/storyboards/generate" in paths
    assert "/api/jobs/{job_id}/storyboards/approve" in paths
    assert "/api/jobs/{job_id}/storyboards/reject" in paths
    assert "/api/jobs/{job_id}/storyboards/regenerate" in paths
