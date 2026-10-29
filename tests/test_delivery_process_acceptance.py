"""#360/#361 actual CORE/Redis processes; controlled external generators only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tests.test_text_redis_process import contour, heartbeat, create, claim, result, get_job, wait
from tests.test_voice_http_acceptance import runtime
from tests import media_harness as harness
from tests.acceptance_provenance import acceptance_provenance
from worker.role_executor import execute_role_task
from tests.test_publisher_durable_reconciliation import _PLATFORMS


def worker(api, node, role, capabilities, supported, **fields):
    from datetime import datetime, timezone
    response = api.post("/api/workers/heartbeat", json={"node_name": node, "role": role,
        "status": "FREE", "vram_mb": 4096, "capabilities": capabilities, "supported_tasks": supported,
        "self_test": {"status": "PASSED", "role": role, "checked_at": datetime.now(timezone.utc).isoformat()}, **fields})
    assert response.status_code == 200, response.text


def take(api, node, supported):
    response = api.post("/api/tasks/claim", json={"node_name": node, "vram_mb": 4096, "supported_tasks": supported})
    assert response.status_code == 200, response.text
    return response.json()["task"]


def submit(api, task, node, artifacts):
    return api.post("/api/tasks/result", json={"job_id": task["job_id"], "task_id": task["task_id"],
        "node_name": node, "success": True, "artifacts": artifacts})


def image(api, task):
    response = api.post("/api/tasks/result", json={"job_id": task["job_id"], "task_id": task["task_id"],
        "node_name": "process-gpu", "success": True, "filename": "frame.png",
        "image_base64": base64.b64encode(harness.make_png()).decode()})
    assert response.status_code == 200, response.text


def prepare_media(contour):
    api = contour["api"]
    heartbeat(api)
    worker(api, "process-gpu", "gpu", ["image_generation"], ["image"])
    job_id = create(api)
    response, _ = result(api, claim(api))
    assert response.status_code == 200, response.text
    assert api.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "process"}).status_code == 200
    wait(lambda: get_job(api, job_id)["status"] == "STORYBOARD_QUEUED")
    heartbeat(api)
    response, _ = result(api, claim(api))
    assert response.status_code == 200, response.text
    preview = take(api, "process-gpu", ["image"])
    assert preview["kind"] == "image_storyboard"
    image(api, preview)
    for endpoint in ("storyboards/images/approve", "storyboards/approve"):
        response = api.post(f"/api/jobs/{job_id}/{endpoint}", json={"version": 1, "image_version": 1, "actor": "process"})
        assert response.status_code == 200, response.text
    for _ in range(150):
        status = get_job(api, job_id)["status"]
        if status in {"TTS_GENERATING", "VIDEO_PENDING_APPROVAL", "VIDEO_GENERATION", "ASSEMBLY"}:
            return job_id
        task = take(api, "process-gpu", ["image"])
        if task:
            image(api, task)
        time.sleep(.1)
    pytest.fail(f"Media did not reach Voice/assembly: {get_job(api, job_id)['status']}")


@pytest.fixture
def voiced(runtime, contour):
    voice = {"provider": "espeak-ng", "model": "espeak-ng", "voice": "uk", "language": "uk", "speed": 160}
    (contour["root"] / "characters" / "processchar" / "voice.json").write_text(json.dumps(voice))
    contour["script_response"]["scenes"][0]["voiceover"] = "Вітаю. Це справжнє озвучення для перевірки конвеєра."
    worker(contour["api"], "process-voice", "voice", ["speech_synthesis"], ["voice"],
           runtime_instance_id="voice-first",
           voice_catalog={"voices": ["uk"], "models": ["espeak-ng"], "providers": ["espeak-ng"], "languages": ["uk"]})
    return contour


@pytest.mark.parametrize("contour,case", [("heartbeat", "normal"), ("heartbeat", "loss"),
    ("lease", "lease"), ("heartbeat", "restart"), ("heartbeat", "core_restart"), ("heartbeat", "cancel")], indirect=["contour"])
def test_actual_voice_audio_assembly_recovery(voiced, case, record_property):
    api = voiced["api"]
    job_id = prepare_media(voiced)
    wait(lambda: get_job(api, job_id)["status"] == "TTS_GENERATING")
    task = take(api, "process-voice", ["voice"])
    assert task and task["task"] == "voice"
    [artifact] = execute_role_task("voice", task)
    if case == "cancel":
        assert api.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        assert submit(api, task, "process-voice", [artifact]).status_code == 409
        assert get_job(api, job_id)["status"] == "CANCELLED"
        return
    if case == "restart":
        voiced["restart"]("core")
        worker(api, "process-voice", "voice", ["speech_synthesis"], ["voice"],
               runtime_instance_id="voice-second", voice_catalog={"voices": ["uk"], "models": ["espeak-ng"]})
        assert submit(api, task, "process-voice", [artifact]).status_code == 409
        wait(lambda: get_job(api, job_id)["status"] == "TTS_GENERATING")
        queue = api.get("/api/tasks/queue").json()
        assert task["task_id"] not in {t["task_id"] for t in queue["ready"] + queue["inflight"]}
    if case == "core_restart":
        voiced["restart"]("core")
        queue = api.get("/api/tasks/queue").json()
        assert task["task_id"] in {t["task_id"] for t in queue["inflight"]}
    if case in {"loss", "lease", "restart"}:
        def replacement_ready():
            queue = api.get("/api/tasks/queue").json()
            return next((t for t in queue["ready"] if t["task"] == "voice" and t["task_id"] != task["task_id"]), None)
        wait(replacement_ready)
        assert submit(api, task, "process-voice", [artifact]).status_code == 409
        worker(api, "process-voice", "voice", ["speech_synthesis"], ["voice"],
               voice_catalog={"voices": ["uk"], "models": ["espeak-ng"]})
        recovered = wait(lambda: take(api, "process-voice", ["voice"]))
        task = recovered
        [artifact] = execute_role_task("voice", task)
    response = submit(api, task, "process-voice", [artifact])
    assert response.status_code == 200, response.text
    assert submit(api, task, "process-voice", [artifact]).status_code == 409
    wait(lambda: get_job(api, job_id)["status"] == "VIDEO_PENDING_APPROVAL")
    job = get_job(api, job_id)
    root = voiced["root"] / "jobs" / job_id
    audio = [a for a in job["artifacts"] if a["kind"] == "audio"]
    assert audio
    audio_data = (root / audio[-1]["path"]).read_bytes()
    assert hashlib.sha256(audio_data).hexdigest() == artifact["contract"]["sha256"]
    version = job["active_video_version"]
    response = api.post(f"/api/jobs/{job_id}/video/approve", json={"actor": "process", "expected_video_version": version})
    assert response.status_code == 200 and response.json()["status"] == "READY", response.text
    record_property("accepted_audio_sha256", artifact["contract"]["sha256"])
    record_property("accepted_video_sha256", job["video_versions"][-1]["sha256"])
    from adapters.ffmpeg import FFmpegAdapter
    decoded = subprocess.run([FFmpegAdapter()._executable(), "-v", "error", "-i", job["output_path"],
        "-map", "0:a:0", "-ac", "1", "-ar", "22050", "-f", "s16le", "-"], capture_output=True, timeout=30)
    assert decoded.returncode == 0, decoded.stderr.decode(errors="replace")
    assert len(decoded.stdout) > 22050 and any(decoded.stdout)
    record_property("accepted_video_audio_pcm_sha256", hashlib.sha256(decoded.stdout).hexdigest())


@pytest.mark.parametrize("channel,cls,credential,metadata,responses", _PLATFORMS)
def test_publisher_process_private_delivery_and_lost_ack_restart(contour, tmp_path, channel, cls, credential, metadata, responses):
    api = contour["api"]
    contour["script_response"]["platforms"] = {channel: metadata}
    job_id = prepare_media(contour)
    wait(lambda: get_job(api, job_id)["status"] == "VIDEO_PENDING_APPROVAL")
    job = get_job(api, job_id)
    approved = api.post(f"/api/jobs/{job_id}/video/approve", json={"actor": "process", "expected_video_version": job["active_video_version"]})
    assert approved.status_code == 200, approved.text
    worker(api, "process-publisher", "publisher", ["publishing"], ["publish"], runtime_instance_id="publisher-first")
    assert api.post(f"/api/jobs/{job_id}/publish", json=[channel]).status_code == 200
    task = take(api, "process-publisher", ["publish"])
    assert task and task["channel"] == channel
    private = tmp_path / "publisher-private"
    private.mkdir()
    env = dict(os.environ, JOB_ROOT=str(private), CONFIG_ROOT=str(private / "config"),
        CORE_ADDRESS=str(api.base_url).rstrip("/"), NODE_NAME="process-publisher",
        NODE_API_TOKEN="text-process-worker-token", PUBLISHER_MOCK="false")
    env[credential] = "controlled-platform-token"
    wire_responses = [{"status": r.status_code, "json": r.json() if r.content else {}, "headers": dict(r.headers)} for r in responses]
    def execute(index, issued=None):
        output = private / f"worker-{index}.json"
        payload = {"task": issued or task, "responses": wire_responses, "output": str(output)}
        process = subprocess.run([sys.executable, "-m", "tests.publisher_process_probe"],
            input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=40)
        assert process.returncode == 0, process.stderr
        return json.loads(output.read_text())
    local_video = private / job_id / "publish-inputs" / Path(task["video_path"]).name
    if channel == "youtube":
        path = f"/api/tasks/{task['task_id']}/inputs/{Path(task['video_path']).name}"
        assert api.get(path, headers={"x-vertep-node": "foreign-worker"}).status_code == 409
        approved_input = api.get(path, headers={"x-vertep-node": "process-publisher"})
        assert approved_input.status_code == 200
        assert len(approved_input.content) == task["delivery_contract"]["size"]
        bad = json.loads(json.dumps(task))
        bad["delivery_contract"]["size"] += 1
        rejected = execute("bad-size", bad)
        assert rejected["http_requests"] == []
        assert "size mismatch" in json.loads(base64.b64decode(rejected["artifacts"][0]["data_base64"]))["error"]
        local_video.write_bytes(b"corrupt")
        rejected = execute("corrupt")
        assert rejected["http_requests"] == []
        assert "sha256 mismatch" in json.loads(base64.b64decode(rejected["artifacts"][0]["data_base64"]))["error"]
        local_video.write_bytes(approved_input.content)
        bad = json.loads(json.dumps(task))
        bad["delivery_contract"]["version"] += 1
        rejected = execute("bad-version", bad)
        assert rejected["http_requests"] == []
    first = execute(1)
    assert first["http_requests"]
    assert local_video.is_file()
    assert hashlib.sha256(local_video.read_bytes()).hexdigest() == task["delivery_contract"]["sha256"]
    # Upload succeeded, but the Worker died before its result reached CORE.
    # CORE and a new Worker process restart; receipt reconciliation must not
    # invoke the external upload transport a second time.
    contour["restart"]("core")
    assert get_job(api, job_id)["status"] == "PUBLISHING"
    # The first process has died; the next heartbeat explicitly proves a new
    # runtime rather than treating a CORE restart as evidence of Worker loss.
    worker(api, "process-publisher", "publisher", ["publishing"], ["publish"], runtime_instance_id="publisher-second")
    reclaimed = take(api, "process-publisher", ["publish"])
    assert reclaimed and reclaimed["task_id"] != task["task_id"]
    task = reclaimed
    second = execute(2)
    assert second["http_requests"] == []
    response = submit(api, reclaimed, "process-publisher", second["artifacts"])
    assert response.status_code == 200, response.text
    published = get_job(api, job_id)
    assert published["status"] == "PUBLISHED"
    receipt = published["publication_results"][channel]
    assert receipt["video_sha256"] == task["delivery_contract"]["sha256"]
    assert submit(api, reclaimed, "process-publisher", first["artifacts"]).status_code == 409
    contour["restart"]("core")
    assert get_job(api, job_id)["publication_results"][channel] == receipt
    third = execute(3)
    assert third["http_requests"] == []
    assert submit(api, reclaimed, "process-publisher", third["artifacts"]).status_code == 409


def test_partial_publish_retry_preserves_successful_channel_across_restart(contour, tmp_path):
    api = contour["api"]
    contour["script_response"]["platforms"] = {"facebook": {"page_id": "controlled-page"}}
    job_id = prepare_media(contour)
    wait(lambda: get_job(api, job_id)["status"] == "VIDEO_PENDING_APPROVAL")
    version = get_job(api, job_id)["active_video_version"]
    assert api.post(f"/api/jobs/{job_id}/video/approve", json={"actor": "process", "expected_video_version": version}).status_code == 200
    worker(api, "process-publisher", "publisher", ["publishing"], ["publish"])
    assert api.post(f"/api/jobs/{job_id}/publish", json=["youtube", "facebook"]).status_code == 200
    private = tmp_path / "partial-worker"
    private.mkdir()
    env = dict(os.environ, JOB_ROOT=str(private), CONFIG_ROOT=str(private / "config"),
        CORE_ADDRESS=str(api.base_url).rstrip("/"), NODE_NAME="process-publisher",
        NODE_API_TOKEN="text-process-worker-token", PUBLISHER_MOCK="false",
        YOUTUBE_ACCESS_TOKEN="controlled-youtube-token", FACEBOOK_ACCESS_TOKEN="controlled-facebook-token")
    def execute(task, name, responses):
        output = private / f"{name}.json"
        payload = {"task": task, "responses": responses, "output": str(output)}
        process = subprocess.run([sys.executable, "-m", "tests.publisher_process_probe"],
            input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=40)
        assert process.returncode == 0, process.stderr
        return json.loads(output.read_text())
    seen = set()
    for _ in range(2):
        issued = take(api, "process-publisher", ["publish"])
        assert issued and issued["channel"] not in seen
        seen.add(issued["channel"])
        if issued["channel"] == "youtube":
            upload = execute(issued, "youtube", [{"status": 200, "headers": {"Location": "https://controlled.invalid/upload"}}, {"status": 200, "json": {"id": "youtube-once"}}])
            assert sum(r["method"] == "PUT" for r in upload["http_requests"]) == 1
        else:
            upload = execute(issued, "facebook-failed", [{"status": 503, "json": {"error": "temporary"}}])
            assert upload["http_requests"]
            started = time.monotonic()
        response = submit(api, issued, "process-publisher", upload["artifacts"])
        assert response.status_code == 200, response.text
    assert seen == {"youtube", "facebook"}
    accepted = get_job(api, job_id)["publication_results"]["youtube"]
    contour["restart"]("core")
    worker(api, "process-publisher", "publisher", ["publishing"], ["publish"])
    retry = wait(lambda: take(api, "process-publisher", ["publish"]))
    assert retry["channel"] == "facebook" and time.monotonic() - started >= 1.9
    assert get_job(api, job_id)["publication_results"]["youtube"] == accepted
    retried = execute(retry, "facebook-success", [{"status": 200, "json": {"id": "facebook-once"}}])
    assert len(retried["http_requests"]) == 1
    assert submit(api, retry, "process-publisher", retried["artifacts"]).status_code == 200
    published = get_job(api, job_id)
    assert published["status"] == "PUBLISHED"
    assert published["publication_results"]["youtube"] == accepted
    assert set(published["published_channels"]) == {"youtube", "facebook"}


def test_core_restart_keeps_live_publisher_lease_and_accepts_its_result(contour):
    api = contour["api"]
    job_id = prepare_media(contour)
    wait(lambda: get_job(api, job_id)["status"] == "VIDEO_PENDING_APPROVAL")
    version = get_job(api, job_id)["active_video_version"]
    assert api.post(f"/api/jobs/{job_id}/video/approve", json={"actor": "process", "expected_video_version": version}).status_code == 200
    worker(api, "process-publisher", "publisher", ["publishing"], ["publish"], runtime_instance_id="still-running")
    assert api.post(f"/api/jobs/{job_id}/publish", json=["youtube"]).status_code == 200
    task = take(api, "process-publisher", ["publish"])
    assert task
    contour["restart"]("core")
    queue = api.get("/api/tasks/queue").json()
    assert queue["total"]["ready"] == 0 and queue["total"]["inflight"] == 1
    assert queue["inflight"][0]["task_id"] == task["task_id"]
    assert take(api, "process-publisher", ["publish"]) is None
    receipt = harness.sandbox_receipt("youtube", task)
    artifact = harness.role_executor._artifact("publication.json", "publication_receipt", json.dumps(receipt).encode())
    response = submit(api, task, "process-publisher", [artifact])
    assert response.status_code == 200, response.text
    assert get_job(api, job_id)["status"] == "PUBLISHED"


def test_lost_upload_response_is_durable_and_never_blindly_uploaded_again(contour, tmp_path):
    api = contour["api"]
    job_id = prepare_media(contour)
    wait(lambda: get_job(api, job_id)["status"] == "VIDEO_PENDING_APPROVAL")
    version = get_job(api, job_id)["active_video_version"]
    assert api.post(f"/api/jobs/{job_id}/video/approve", json={"actor": "process", "expected_video_version": version}).status_code == 200
    worker(api, "process-publisher", "publisher", ["publishing"], ["publish"])
    assert api.post(f"/api/jobs/{job_id}/publish", json=["youtube"]).status_code == 200
    task = take(api, "process-publisher", ["publish"])
    private = tmp_path / "unknown-upload-worker"
    private.mkdir()
    env = dict(os.environ, JOB_ROOT=str(private), CONFIG_ROOT=str(private / "config"),
        CORE_ADDRESS=str(api.base_url).rstrip("/"), NODE_NAME="process-publisher",
        NODE_API_TOKEN="text-process-worker-token", PUBLISHER_MOCK="false", YOUTUBE_ACCESS_TOKEN="controlled-token")
    def execute(name):
        output = private / f"{name}.json"
        payload = {"task": task, "output": str(output), "disconnect_after_receive": True,
            "responses": [{"status": 200, "headers": {"Location": "https://controlled.invalid/session"}}, {"status": 200}]}
        process = subprocess.run([sys.executable, "-m", "tests.publisher_process_probe"], input=json.dumps(payload),
            text=True, capture_output=True, env=env, timeout=40)
        assert process.returncode == 0, process.stderr
        return json.loads(output.read_text())
    lost = execute("received-but-response-lost")
    assert sum(r["method"] == "PUT" for r in lost["http_requests"]) == 1
    receipt = json.loads(base64.b64decode(lost["artifacts"][0]["data_base64"]))
    assert receipt["outcome_unknown"] and not receipt["retryable"]
    assert submit(api, task, "process-publisher", lost["artifacts"]).status_code == 200
    assert "youtube" in get_job(api, job_id)["publish_intent"]
    contour["restart"]("core")
    replay = execute("new-worker-process")
    assert replay["http_requests"] == [] and "reconciliation required" in replay["error"]
    assert get_job(api, job_id)["publication_results"]["youtube"]["outcome_unknown"]
