"""#352: HTTP review guards and actual Resume/Retry retain version boundaries."""
import pytest
import json
import base64

from core.state import store, executor
from tests.acceptance_provenance import acceptance_provenance
from tests.test_full_media_harness import client, fleet, harness


@pytest.mark.parametrize("action", ["resume", "retry"])
@pytest.mark.parametrize("condition", ["missing", "mismatched", "unapproved"])
def test_resume_retry_new_cannot_bypass_active_storyboard(fleet, monkeypatch, action, condition):
    api = fleet
    response = api.post("/api/jobs", json={"topic": "Video storyboard gate", "character_id": "harnesschar", "task_type": "video"})
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    harness.approve_script_only(api, job_id, monkeypatch)
    harness.run_storyboard_task(api, job_id, monkeypatch)
    harness.submit_storyboard_previews(api, job_id, count=2)
    harness.wait_for_status(api, job_id, "STORYBOARD_READY")
    job = store.jobs[job_id]
    if condition == "missing":
        job.storyboards = []
        job.active_storyboard_version = None
    elif condition == "mismatched":
        job.active_storyboard_version += 1
    store.repository.save_job(job)
    assert api.post(f"/api/jobs/{job_id}/pause").status_code == 200
    response = api.post(f"/api/jobs/{job_id}/{action}")
    assert response.status_code == 200, response.text
    # Drain the actual CORE executor before inspecting persisted outcomes.
    for _ in range(2):
        executor.submit(lambda: None).result(timeout=20)
    result = harness.get_job(api, job_id)
    assert result["status"] == "NEW"
    assert result["active_video_version"] in (None, 0)
    assert not job.active_task_id
    restored = harness.restart_store().jobs[job_id]
    assert restored.active_storyboard_version == job.active_storyboard_version
    assert restored.status.value == "NEW"


def test_stale_image_and_storyboard_approval_do_not_change_persisted_files(fleet, monkeypatch):
    api = fleet
    job_id = harness.create_job(api)
    harness.approve_script_only(api, job_id, monkeypatch)
    harness.run_storyboard_task(api, job_id, monkeypatch)
    harness.submit_storyboard_previews(api, job_id, count=2)
    harness.wait_for_status(api, job_id, "STORYBOARD_READY")
    board = harness.active_storyboard(api, job_id)
    def snapshot():
        return {p.relative_to(harness.job_root(job_id)).as_posix(): p.read_bytes()
                for p in harness.job_root(job_id).rglob("*.json")}
    before = snapshot()
    assert before, "Persisted JSON must exist for the mutation guard proof"
    for path, payload in (
        ("images/approve", {"version": board["version"], "image_version": board["image_version"] + 1}),
        ("approve", {"version": board["version"] + 1}),
    ):
        response = api.post(f"/api/jobs/{job_id}/storyboards/{path}", json={"actor": "acceptance", **payload})
        assert response.status_code == 409, response.text
        assert snapshot() == before
    from core.first_run import password_hash
    auth = "Basic " + base64.b64encode(b"review-viewer:review-viewer-password").decode()
    with monkeypatch.context() as context:
        context.setenv("USERS_JSON", json.dumps({"review-viewer": {
            "password_hash": password_hash("review-viewer-password"), "role": "viewer"}}))
        response = api.post(f"/api/jobs/{job_id}/storyboards/images/approve", headers={"Authorization": auth},
            json={"version": board["version"], "image_version": board["image_version"], "actor": "viewer"})
        assert response.status_code == 403, response.text
    assert snapshot() == before
