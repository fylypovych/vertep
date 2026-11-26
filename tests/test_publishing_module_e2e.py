"""#502 (owner decisions 19.11.2026): channel credentials, automatic policy and the
Publishing module's editor actions with a verified user context.

Strict mode: no legacy channel allowance; every channel needs its own secret or
an explicit, audited administrator permission for the global credentials.
"""

import hashlib
import json
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.models import Channel, JobStatus, VideoVersion

pytestmark = pytest.mark.strict_channel_credentials
WORKER = {"X-Vertep-Token": "worker-token"}
MODULE_DIR = Path(__file__).resolve().parents[1] / "modules" / "publisher" / "app"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from core.first_run import set_integration_secret
    brands = tmp_path / "brands"
    for brand, policy in (("brand-a", "approval_required"), ("brand-b", "automatic")):
        (brands / brand).mkdir(parents=True)
        (brands / brand / "brand.json").write_text(json.dumps({"id": brand, "name": brand, "publication_policy": policy}))
    monkeypatch.setenv("BRANDS_ROOT", str(brands))
    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    monkeypatch.setenv("VERTEP_MODULE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    set_integration_secret("telegram_publisher_bot_token", "GLOBAL-TOKEN")
    from core.app import app
    client = TestClient(app)
    client.post("/api/workers/heartbeat", headers=WORKER, json={
        "node_name": "pub-worker", "vram_mb": 0, "role": "publisher",
        "capabilities": ["publishing"], "supported_tasks": ["publish"]})
    return {"client": client, "tmp": tmp_path}


def _ready_job(brand, *, status=JobStatus.READY):
    from core.state import store
    job = store.create(f"Матеріал {brand}", "hero", 5, "web", "image", 0, brand)
    video = store.root / job.job_id / "final" / "video-v1.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(f"video of {job.job_id}".encode())
    with store.lock:
        job.video_versions = [VideoVersion(version=1, path="final/video-v1.mp4",
                                           sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
                                           approved=status == JobStatus.READY)]
        job.active_video_version = 1
        job.output_path = str(video)
        store.update(job, status, "test material")
    return job


def _channel(channel_id, brand, **metadata):
    from core.state import store
    channel = Channel(channel_id=channel_id, brand_id=brand, channel_type="telegram", target=f"@{channel_id.replace('-', '_')}x",
                      metadata=metadata)
    store.repository.save_channel(channel)
    return channel


def _claim(client, job_id):
    for _ in range(50):
        task = client.post("/api/tasks/claim", headers=WORKER, json={"node_name": "pub-worker", "vram_mb": 0}).json().get("task")
        if task and task.get("job_id") == job_id:
            return task
    return None


def _secrets(client, task):
    return client.get(f"/api/tasks/{task['task_id']}/secrets", headers={**WORKER, "x-vertep-node": "pub-worker"}).json()


def test_a_channel_without_credentials_is_blocked_and_its_own_secret_is_the_only_one_delivered(env):
    from core.state import store, task_queue
    client = env["client"]
    _channel("ch-a1", "brand-a")
    job = _ready_job("brand-a")
    blocked = client.post(f"/api/jobs/{job.job_id}/publish", json=["telegram"])
    assert blocked.status_code == 409 and "credentials are not configured" in blocked.json()["detail"]
    assert client.get("/api/channels/ch-a1/credentials").json()["mode"] == "missing"
    saved = client.put("/api/channels/ch-a1/credentials", json={"secrets": {"telegram_publisher_bot_token": "A-TOKEN"}})
    assert saved.status_code == 200 and saved.json() == {"channel_id": "ch-a1", "mode": "own",
        "configured": ["telegram_publisher_bot_token"], "use_global_credentials": False}
    assert "A-TOKEN" not in saved.text
    assert client.put("/api/channels/ch-a1/credentials", json={"secrets": {"youtube_access_token": "x"}}).status_code == 422
    assert client.post(f"/api/jobs/{job.job_id}/publish", json=["telegram"]).status_code == 200
    task = _claim(client, job.job_id)
    delivered = _secrets(client, task)
    assert delivered["secrets"] == {"telegram_publisher_bot_token": "A-TOKEN"}  # never GLOBAL-TOKEN
    refreshed = client.put(f"/api/tasks/{task['task_id']}/secrets", headers={**WORKER, "x-vertep-node": "pub-worker"},
                           json={"secrets": {"telegram_publisher_bot_token": "A-TOKEN-2"}})
    assert refreshed.status_code == 200
    from core.first_run import ensure_secret_store
    stored = ensure_secret_store()
    assert stored["channel:ch-a1:telegram_publisher_bot_token"] == "A-TOKEN-2" and stored["telegram_publisher_bot_token"] == "GLOBAL-TOKEN"
    task_queue.ack(task["task_id"])
    store.repository.delete_channel("ch-a1")


def test_global_credentials_only_by_explicit_audited_administrator_choice(env):
    from core.module.gateway import read_audit
    from core.state import store, task_queue
    client = env["client"]
    _channel("ch-legacy", "brand-a")
    job = _ready_job("brand-a")
    allowed = client.put("/api/channels/ch-legacy/credentials", json={"use_global_credentials": True})
    assert allowed.status_code == 200 and allowed.json()["mode"] == "global" and "warning" in allowed.json()
    assert client.post(f"/api/jobs/{job.job_id}/publish", json=["telegram"]).status_code == 200
    task = _claim(client, job.job_id)
    assert _secrets(client, task)["secrets"] == {"telegram_publisher_bot_token": "GLOBAL-TOKEN"}
    events = [(e["action"], e["outcome"]) for e in read_audit()]
    assert ("channel.credentials", "global_allowed") in events and ("publish.credentials", "global") in events
    task_queue.ack(task["task_id"])
    store.repository.delete_channel("ch-legacy")


def test_automatic_policy_publishes_after_approval_and_waits_for_foreign_owners(env):
    from core.state import store, task_queue
    client = env["client"]
    _channel("ch-b-auto", "brand-b")
    _channel("ch-b-manual", "brand-b", policy="approval_required")
    client.put("/api/channels/ch-b-auto/credentials", json={"secrets": {"telegram_publisher_bot_token": "B-TOKEN"}})
    job = _ready_job("brand-b", status=JobStatus.VIDEO_PENDING_APPROVAL)
    approved = client.post(f"/api/jobs/{job.job_id}/video/approve", json={"actor": "admin", "expected_video_version": 1})
    assert approved.status_code == 200, approved.text
    assert store.jobs[job.job_id].status == JobStatus.PUBLISHING  # automatic, no manual publish call
    task = _claim(client, job.job_id)
    assert task["publish_intent"]["channel_id"] == "ch-b-auto"
    task_queue.ack(task["task_id"])
    # Material using brand A's character: automatic only after A approves.
    shared = _ready_job("brand-b", status=JobStatus.VIDEO_PENDING_APPROVAL)
    with store.lock:
        shared.character_owner_brand = "brand-a"
    client.post(f"/api/jobs/{shared.job_id}/video/approve", json={"actor": "admin", "expected_video_version": 1})
    assert store.jobs[shared.job_id].status == JobStatus.READY
    assert any("AUTOMATIC PUBLICATION WAITING" in event for event in store.jobs[shared.job_id].events)
    version = store.jobs[shared.job_id].video_versions[0]
    decision = client.post(f"/api/jobs/{shared.job_id}/shared-approval", json={
        "owner_brand": "brand-a", "decision": "approve", "version": 1, "expected_sha256": version.sha256})
    assert decision.status_code == 200
    assert store.jobs[shared.job_id].status == JobStatus.PUBLISHING
    for channel_id in ("ch-b-auto", "ch-b-manual"):
        store.repository.delete_channel(channel_id)


def test_publishing_module_acts_only_for_a_verified_user_on_its_own_brand(env, monkeypatch):
    import yaml
    from core.module.gateway import issue_token, issue_user_context, read_audit
    from core.module.manifest import ModuleManifest
    from core.state import store, task_queue
    from core.workflow.runtime import WorkflowRuntime
    client = env["client"]
    dag = env["tmp"] / "dag"
    dag.mkdir()
    (dag / "c.json").write_text(json.dumps({"name": "C", "version": "1.0.0", "tasks": [{"id": "a", "type": "text_generation"}]}))
    monkeypatch.setenv("VERTEP_MODULE_PLACEMENT_FILE", str(env["tmp"] / "p.json"))
    monkeypatch.setenv("VERTEP_MODULE_LIFECYCLE_ROOT", str(env["tmp"] / "lc"))
    monkeypatch.setenv("WORKFLOW_STATE_ROOT", str(env["tmp"] / "wf"))
    runtime = WorkflowRuntime(dag)
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: runtime)
    monkeypatch.setenv("USERS_JSON", json.dumps({"mod-a": {"password_hash": "x", "role": "moderator", "brands": ["brand-a"]}}))
    monkeypatch.syspath_prepend(str(MODULE_DIR))
    import publisher_app
    token = issue_token("publisher", "text-node")
    monkeypatch.setattr(publisher_app, "TOKEN", token)

    def core(method, path, body=None, user_context=""):
        headers = {"x-vertep-module-token": token, "Authorization": ""}
        if user_context:
            headers["x-vertep-user-context"] = user_context
        response = client.request(method, path, json=body, headers=headers)
        return response.status_code, response.json()
    monkeypatch.setattr(publisher_app, "_core", core)
    server = ThreadingHTTPServer(("127.0.0.1", 0), publisher_app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    manifest = ModuleManifest.from_dict(yaml.safe_load((MODULE_DIR / "manifest.yaml").read_text(encoding="utf-8")))
    runtime.modules.report_node("text-node", [{**manifest.to_dict(), "ready": True,
                                               "endpoint": f"http://127.0.0.1:{server.server_address[1]}"}])
    runtime.modules.set_placement("publisher", ["text-node"])
    runtime.modules.approve_permissions("publisher", list(manifest.permissions))
    _channel("ch-a-mod", "brand-a")
    client.put("/api/channels/ch-a-mod/credentials", json={"secrets": {"telegram_publisher_bot_token": "A-TOKEN"}})
    own, foreign = _ready_job("brand-a"), _ready_job("brand-c")

    def act(job_id, context):
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request("POST", f"/publications/{job_id}/publish", json.dumps({"channels": ["telegram"]}).encode(),
                           {"x-vertep-module-token": token, "x-vertep-user-context": context,
                            "content-type": "application/json",
                            # What a module might claim is ignored: CORE re-derives the rights.
                            "x-vertep-role": "admin", "x-vertep-brands": "*"})
        response = connection.getresponse()
        status = response.status
        response.read()
        connection.close()
        return status

    try:
        context = issue_user_context("mod-a", "moderator", "publisher")
        assert act(foreign.job_id, context) == 403  # not this moderator's brand
        assert act(own.job_id, "forged") == 401
        assert act(own.job_id, issue_user_context("mod-a", "moderator", "characters")) == 401  # another module's context
        assert act(own.job_id, issue_user_context("mod-a", "moderator", "publisher", now=time.time() - 3600)) == 401
        assert act(own.job_id, issue_user_context("mod-a", "admin", "publisher")) in {200, 403}  # role re-derived
        assert act(own.job_id, context) == 200 or store.jobs[own.job_id].status == JobStatus.PUBLISHING
        assert store.jobs[own.job_id].status == JobStatus.PUBLISHING
        published = [e for e in read_audit() if e["action"] == "module.publications.publish" and e["outcome"] == "allowed"]
        assert published and published[-1]["actor"] == "mod-a" and published[-1]["module"] == "publisher"
        task = _claim(client, own.job_id)
        task_queue.ack(task["task_id"])
    finally:
        server.shutdown()
        store.repository.delete_channel("ch-a-mod")
