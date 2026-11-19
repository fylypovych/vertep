"""#500 Shared content: cross-brand approval before publication.

Brand A's material uses a character owned by brand B. A may generate; to
publish, B's moderator (or the administrator) approves the exact immutable
video version. Pending never publishes; revocation stops sends that have not
started; a changed file needs a new approval; decisions keep their history.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.models import JobStatus, VideoVersion
from core.telegram.handlers import store

WORKER = {"X-Vertep-Token": "worker-token"}


def _session(monkeypatch, users, user):
    from core.security import _session_token, session_secret
    monkeypatch.setenv("USERS_JSON", json.dumps(users))
    session = _session_token(user, users[user]["role"])
    secret = session_secret()
    csrf = hmac.new(secret.encode() if isinstance(secret, str) else secret, session.encode(), hashlib.sha256).hexdigest()
    client = TestClient(app, headers={"Authorization": "", "x-csrf-token": csrf})
    client.cookies.set("vertep_session", session)
    client.cookies.set("vertep_csrf", csrf)
    return client


@pytest.fixture
def shared(monkeypatch):
    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    users = {"mod-a": {"password_hash": "x", "role": "moderator", "brands": ["brand-a"]},
             "mod-b": {"password_hash": "x", "role": "moderator", "brands": ["brand-b"]}}
    a = _session(monkeypatch, users, "mod-a")
    b = _session(monkeypatch, users, "mod-b")
    created = a.post("/api/jobs", json={"topic": "Спільний контент", "brand_id": "brand-a"})
    assert created.status_code == 200, created.text
    job = store.jobs[created.json()["job_id"]]
    import time
    from core.state import task_queue
    deadline = time.monotonic() + 10
    while (not any("SCRIPT TASK QUEUED" in event for event in store.jobs[job.job_id].events)
           and time.monotonic() < deadline):
        time.sleep(0.02)  # let the creation dispatch settle before parking the job
    for task in task_queue.ready_tasks() + task_queue.inflight_tasks():
        if task.get("job_id") == job.job_id:
            task_queue.discard(task["task_id"])
    # The material is ready: an approved immutable version of brand A's job that
    # uses brand B's character (snapshot taken from the Characters module).
    video = store.root / job.job_id / "final" / "video-v1.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"approved video v1")
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    with store.lock:
        job.character_owner_brand = "brand-b"
        job.video_versions = [VideoVersion(version=1, path="final/video-v1.mp4", sha256=digest, approved=True,
                                           approved_by="mod-a")]
        job.active_video_version = 1
        job.output_path = str(video)
        store.update(job, JobStatus.READY, "READY (test material)")
    TestClient(app).post("/api/workers/heartbeat", headers=WORKER, json={
        "node_name": "pub-worker", "vram_mb": 0, "role": "publisher",
        "capabilities": ["publishing"], "supported_tasks": ["publish"]})
    return {"a": a, "b": b, "job": job, "digest": digest, "video": video}


def _claim_publish(job_id):
    client = TestClient(app)
    for _ in range(50):
        task = client.post("/api/tasks/claim", json={"node_name": "pub-worker", "vram_mb": 0}, headers=WORKER).json().get("task")
        if task is None:
            return None
        if task.get("task") == "publish" and task.get("job_id") == job_id:
            return task
    return None


def test_pending_blocks_publication_and_only_the_owner_brand_decides(shared):
    a, b, job = shared["a"], shared["b"], shared["job"]
    blocked = a.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"])
    assert blocked.status_code == 409 and "brand-b" in blocked.json()["detail"]
    assert a.get("/api/shared-content", params={"brand_id": "brand-b"}).status_code == 403
    listed = b.get("/api/shared-content", params={"brand_id": "brand-b"}).json()["items"]
    assert [item["job_id"] for item in listed] == [job.job_id] and listed[0]["owners"] == {"brand-b": "pending"}
    body = {"owner_brand": "brand-b", "decision": "approve", "version": 1, "expected_sha256": shared["digest"]}
    assert a.post(f"/api/jobs/{job.job_id}/shared-approval", json=body).status_code == 403
    stale = b.post(f"/api/jobs/{job.job_id}/shared-approval", json={**body, "expected_sha256": "0" * 64})
    assert stale.status_code == 409
    approved = b.post(f"/api/jobs/{job.job_id}/shared-approval", json=body)
    assert approved.status_code == 200 and approved.json()["owners"] == {"brand-b": "approved"}
    record = approved.json()["history"][-1]
    assert record["actor"] == "mod-b" and record["sha256"] == shared["digest"] and record["owner_brand"] == "brand-b"
    published = a.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"])
    assert published.status_code == 200 and published.json()["status"] == "PUBLISHING", published.text
    assert b.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"]).status_code == 403  # not B's job


def test_revoke_stops_a_send_that_has_not_started_and_needs_a_new_approval(shared):
    a, b, job = shared["a"], shared["b"], shared["job"]
    body = {"owner_brand": "brand-b", "decision": "approve", "version": 1, "expected_sha256": shared["digest"]}
    assert b.post(f"/api/jobs/{job.job_id}/shared-approval", json=body).status_code == 200
    assert a.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"]).json()["status"] == "PUBLISHING"
    revoked = b.post(f"/api/jobs/{job.job_id}/shared-approval/revoke", json={"owner_brand": "brand-b"})
    assert revoked.status_code == 200 and revoked.json()["owners"] == {"brand-b": "revoked"}
    assert _claim_publish(job.job_id) is None  # the queued send never starts
    assert store.jobs[job.job_id].status == JobStatus.READY and not store.jobs[job.job_id].publish_task_ids
    assert a.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"]).status_code == 409
    assert b.post(f"/api/jobs/{job.job_id}/shared-approval", json=body).status_code == 200  # a new approval
    assert a.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"]).status_code == 200
    started = _claim_publish(job.job_id)
    assert started is not None  # handed out while approved: this send has started
    b.post(f"/api/jobs/{job.job_id}/shared-approval/revoke", json={"owner_brand": "brand-b"})
    assert store.jobs[job.job_id].publish_task_ids.get(started["task_id"]) == "youtube"  # it may finish
    decisions = [item["decision"] for item in store.jobs[job.job_id].shared_approvals]
    assert decisions == ["approve", "revoke", "approve", "revoke"]


def test_changed_file_or_version_needs_a_new_approval_and_history_survives_reload(shared):
    from core.models import Job
    a, b, job = shared["a"], shared["b"], shared["job"]
    body = {"owner_brand": "brand-b", "decision": "approve", "version": 1, "expected_sha256": shared["digest"]}
    assert b.post(f"/api/jobs/{job.job_id}/shared-approval", json=body).status_code == 200
    shared["video"].write_bytes(b"tampered after approval")
    changed = a.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"])
    assert changed.status_code == 409 and "hash" in changed.json()["detail"]
    reloaded = Job.model_validate_json(job.model_dump_json())  # persistence round trip
    assert reloaded.character_owner_brand == "brand-b"
    assert reloaded.shared_approvals == job.shared_approvals and reloaded.shared_approvals[0]["actor"] == "mod-b"


def test_material_of_the_own_brand_needs_no_shared_approval(shared):
    a, job = shared["a"], shared["job"]
    with store.lock:
        job.character_owner_brand = None
    assert a.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"]).status_code == 200
