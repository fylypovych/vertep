"""Task 1: mid-render controls gates — retry and regenerate are refused (409) while a render
owns the Job, and accepted again once the render released it (core/api/jobs.py)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.models import JobStatus
from core.state import store


@pytest.fixture
def rendering_job(monkeypatch):
    import core.api.jobs as jobs_api
    submitted = []
    monkeypatch.setattr(jobs_api, "executor", SimpleNamespace(submit=lambda *args: submitted.append(args)))
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Render controls"}).json()["job_id"]
    job = store.jobs[job_id]
    job.status = JobStatus.ASSEMBLY
    job.assembly_task_ids = {"assembly-1": 1}
    submitted.clear()
    return client, job, submitted


def _release(job):
    job.assembly_task_ids = {}
    job.status = JobStatus.FAILED


@pytest.mark.parametrize("action", ["retry", "regenerate"])
def test_action_blocked_mid_render_and_allowed_after_release(rendering_job, action):
    client, job, submitted = rendering_job
    response = client.post(f"/api/jobs/{job.job_id}/{action}")
    assert response.status_code == 409 and "render in progress" in response.json()["detail"]
    assert job.status == JobStatus.ASSEMBLY and submitted == []  # nothing was dispatched

    _release(job)
    response = client.post(f"/api/jobs/{job.job_id}/{action}")
    assert response.status_code == 200 and response.json()["status"] == "NEW"
    assert len(submitted) == 1  # the job is dispatched again


@pytest.mark.parametrize("action", ["retry", "regenerate"])
def test_action_blocked_while_the_video_regenerates(rendering_job, action):
    client, job, submitted = rendering_job
    _release(job)
    job.video_regenerating = True
    assert client.post(f"/api/jobs/{job.job_id}/{action}").status_code == 409
    assert submitted == []
