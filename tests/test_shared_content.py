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


def _approve(client, job, owner, digest):
    return client.post(f"/api/jobs/{job.job_id}/shared-approval",
                       json={"owner_brand": owner, "decision": "approve", "version": 1, "expected_sha256": digest})


def test_every_foreign_owner_must_approve(shared, monkeypatch):
    """AC4: material using characters of brands B and C needs both decisions."""
    from tests.test_shared_content import _session
    a, b, job = shared["a"], shared["b"], shared["job"]
    with store.lock:
        job.character_owner_brands = ["brand-b", "brand-c"]
    c = _session(monkeypatch, {"mod-c": {"password_hash": "x", "role": "moderator", "brands": ["brand-c"]},
                               "mod-b": {"password_hash": "x", "role": "moderator", "brands": ["brand-b"]},
                               "mod-a": {"password_hash": "x", "role": "moderator", "brands": ["brand-a"]}}, "mod-c")
    assert _approve(b, job, "brand-b", shared["digest"]).json()["owners"] == {"brand-b": "approved", "brand-c": "pending"}
    blocked = a.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"])
    assert blocked.status_code == 409 and "brand-c" in blocked.json()["detail"]
    assert _approve(c, job, "brand-c", shared["digest"]).json()["owners"] == {"brand-b": "approved", "brand-c": "approved"}
    assert a.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"]).status_code == 200


def test_owner_publishes_the_same_artifact_independently_with_its_own_texts(shared):
    """AC5/AC6: B gets its own job with the identical file and its own metadata;
    no automatic cross-post; revoking or changing the source stops B's unsent copy."""
    import hashlib
    a, b, job = shared["a"], shared["b"], shared["job"]
    early = b.post(f"/api/jobs/{job.job_id}/shared-publications", json={"brand_id": "brand-b", "title": "B"})
    assert early.status_code == 409  # pending: nothing to publish yet
    assert _approve(b, job, "brand-b", shared["digest"]).status_code == 200
    assert a.post(f"/api/jobs/{job.job_id}/shared-publications", json={"brand_id": "brand-b"}).status_code == 403
    created = b.post(f"/api/jobs/{job.job_id}/shared-publications", json={
        "brand_id": "brand-b", "title": "Наша версія", "description": "Опис бренду B", "hashtags": ["#b"]})
    assert created.status_code == 200, created.text
    derived = store.jobs[created.json()["job_id"]]
    assert derived.brand_id == "brand-b" and derived.status == JobStatus.READY and derived.topic == "Наша версія"
    copy = store.root / derived.job_id / derived.video_versions[0].path
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == shared["digest"] == derived.shared_source["sha256"]
    assert derived.publication_metadata == {"title": "Наша версія", "description": "Опис бренду B", "hashtags": ["#b"]}
    assert store.jobs[job.job_id].shared_publications[-1]["actor"] == "mod-b"
    assert not store.jobs[job.job_id].publish_task_ids  # no automatic cross-post of A's job
    published = b.post(f"/api/jobs/{derived.job_id}/publish", json=["youtube"])
    assert published.status_code == 200 and published.json()["status"] == "PUBLISHING"
    task = _claim_publish(derived.job_id)
    assert task["metadata"]["description"] == "Опис бренду B" and task["metadata"]["brand_id"] == "brand-b"
    # A second owner withdraws: B's next, not yet started send is stopped.
    with store.lock:
        job.character_owner_brands = ["brand-b", "brand-c"]
    assert b.post(f"/api/jobs/{derived.job_id}/publish", json=["youtube"]).status_code == 409


def test_revoke_and_send_start_race_never_hands_out_a_send_after_the_revocation(shared):
    """AC6/AC7/AC8: concurrent revoke vs claim. Either the send started first (it may
    finish) or the revocation came first (the send never starts)."""
    import threading
    a, b, job = shared["a"], shared["b"], shared["job"]
    for _ in range(5):
        assert _approve(b, job, "brand-b", shared["digest"]).status_code == 200
        with store.lock:
            job.publish_task_ids.clear()
            store.update(job, JobStatus.READY, "READY (race round)")
        assert a.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"]).status_code == 200
        results = {}
        threads = [threading.Thread(target=lambda: results.__setitem__("task", _claim_publish(job.job_id))),
                   threading.Thread(target=lambda: results.__setitem__("revoke", b.post(
                       f"/api/jobs/{job.job_id}/shared-approval/revoke", json={"owner_brand": "brand-b"})))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results["revoke"].status_code == 200
        events = store.jobs[job.job_id].events
        revoked_at = max(i for i, event in enumerate(events) if "APPROVAL REVOKED" in event)
        claimed = [i for i, event in enumerate(events) if "PUBLISH CLAIMED" in event]
        if results["task"] is not None:
            assert claimed and claimed[-1] < revoked_at  # started before the revocation
        else:
            assert not claimed or claimed[-1] < revoked_at
        from core.state import task_queue
        if results["task"] is not None:
            task_queue.ack(results["task"]["task_id"])
            store.workers.get("pub-worker", {}).update({"status": "READY", "current_task": None, "current_job": None})
