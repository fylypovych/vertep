"""#358/#359: actual Redis + CORE process restarts and Text HTTP task lifecycle.

Only the external Ollama HTTP server is controlled. CORE queue, persistence,
claims/results, watchdog and Worker execute_role_task are unchanged.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import redis

from tests.acceptance_provenance import acceptance_provenance
from tests import media_harness as harness
from worker.role_executor import execute_role_task

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "text-process-worker-token"
NODE = "text-process"


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait(predicate, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.1)
    pytest.fail("Timed out at actual Text/Redis process boundary")


@pytest.fixture
def contour(tmp_path, monkeypatch, request):
    binary = os.getenv("VERTEP_REDIS_SERVER") or shutil.which("redis-server")
    if not binary:
        if os.getenv("VERTEP_REQUIRE_TEXT_REDIS_PROCESS") == "1":
            pytest.fail("Actual redis-server is required; local fallback is not acceptance")
        pytest.skip("Actual redis-server is unavailable")
    redis_port, core_port = port(), port()
    redis_dir = tmp_path / "redis"
    redis_dir.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    (config / "users.json").write_text("{}", encoding="utf-8")
    (config / "installation.json").write_text('{"completed_at":"2026-01-01"}', encoding="utf-8")
    characters = tmp_path / "characters"
    harness.write_character(characters, "processchar", voice={"provider": "mock", "voice": "uk",
                                                           "model": "process-model", "language": "uk"})
    requests = []
    script_response = {"title": "Controlled Text output", "description": "", "hashtags": [],
                       "scenes": [{"prompt": "controlled image", "video_prompt": "controlled video",
                                   "voiceover": "", "duration": 4}]}
    class Ollama(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            script = script_response
            data = json.dumps({"response": json.dumps(script)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    ollama = ThreadingHTTPServer(("127.0.0.1", 0), Ollama)
    threading.Thread(target=ollama.serve_forever, daemon=True).start()
    ollama_url = f"http://127.0.0.1:{ollama.server_port}"
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("OLLAMA_URL", ollama_url)
    monkeypatch.setenv("OLLAMA_MODEL", "process-model")
    monkeypatch.setenv("OLLAMA_SCRIPT_MODEL", "process-model")
    monkeypatch.setenv("STORYBOARD_TARGET_DURATION", "4")
    processes, logs = {}, []
    redis_url = f"redis://127.0.0.1:{redis_port}/0"
    r = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=2)
    env = dict(os.environ, REDIS_URL=redis_url, DATABASE_URL="", CONFIG_ROOT=str(config),
        JOB_ROOT=str(tmp_path / "jobs"), STORAGE_ROOT=str(tmp_path / "storage"),
        UPDATE_STATE_DIR=str(tmp_path / "update"), SYSTEM_STATE_BACKEND="file",
        CHARACTERS_ROOT=str(characters), BRANDS_ROOT=str(tmp_path / "brands"),
        WORKFLOWS_ROOT=str(tmp_path / "workflows"), NODE_API_TOKEN=TOKEN,
        ADMIN_USER="admin", ADMIN_PASSWORD="process-admin-password", USERS_JSON="",
        LOCAL_WORKER_FALLBACK="false", REQUIRE_WORKER_SELF_TEST="true",
        WATCHDOG_INTERVAL="0.2", HEARTBEAT_TIMEOUT="60" if getattr(request, "param", "heartbeat") == "lease" else "15", TASK_LEASE_SECONDS="15",
        OLLAMA_STORYBOARD_MAX_RETRIES="2", OLLAMA_SCRIPT_MAX_RETRIES="2", MAX_RETRIES="1",
        LOG_ROOT=str(tmp_path / "logs"), AUTO_CLEANUP="false")
    auth = "Basic " + base64.b64encode(b"admin:process-admin-password").decode()
    api = httpx.Client(base_url=f"http://127.0.0.1:{core_port}", timeout=20,
                       headers={"Authorization": auth, "x-vertep-token": TOKEN})
    def start(name):
        log = (tmp_path / f"{name}-{len(logs)}.log").open("w", encoding="utf-8")
        logs.append(log)
        cmd = ([binary, "--bind", "127.0.0.1", "--port", str(redis_port), "--dir", str(redis_dir),
                "--save", "", "--appendonly", "yes", "--appendfsync", "always"] if name == "redis" else
               [sys.executable, "-m", "uvicorn", "core.app:app", "--host", "127.0.0.1", "--port", str(core_port)])
        processes[name] = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        def ready():
            assert processes[name].poll() is None, f"{name} exited; see disposable log"
            try:
                return r.ping() if name == "redis" else api.get("/api/session").status_code == 200
            except (redis.RedisError, httpx.HTTPError):
                return False
        wait(ready)
    def stop(name):
        process = processes.get(name)
        if process and process.poll() is None:
            if name == "redis":
                try:
                    r.execute_command("SHUTDOWN", "NOSAVE")
                except redis.RedisError:
                    pass
            else:
                process.terminate()
            process.wait(timeout=15)
    def restart(name, *, crash=False):
        if crash:
            processes[name].kill()
            processes[name].wait(timeout=15)
        else:
            stop(name)
        start(name)
    try:
        start("redis")
        start("core")
        assert api.get("/api/status").json()["queue"]["backend"] == "redis"
        yield {"api": api, "redis": r, "restart": restart, "stop": stop, "start": start,
               "requests": requests, "root": tmp_path, "script_response": script_response,
               "loss": getattr(request, "param", "heartbeat"),
               "redis_identity": r.info("server")["redis_version"],
               "redis_binary_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest()}
    finally:
        stop("core")
        stop("redis")
        api.close()
        ollama.shutdown()
        ollama.server_close()
        for log in logs:
            log.close()


def heartbeat(api, *, passed=True, node=NODE):
    body = {"node_name": node, "role": "text", "status": "FREE", "vram_mb": 0,
            "capabilities": ["text_generation"], "supported_tasks": ["text"],
            "voice_catalog": {"voices": ["uk"], "models": ["process-model"]}}
    if passed:
        body["self_test"] = {"status": "PASSED", "role": "text", "checked_at": datetime.now(timezone.utc).isoformat()}
    response = api.post("/api/workers/heartbeat", json=body)
    assert response.status_code == 200, response.text


def create(api):
    response = api.post("/api/jobs", json={"topic": "Process Text", "character_id": "processchar"})
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    wait(lambda: get_job(api, job_id)["status"] == "SCRIPT_QUEUED")
    return job_id


def get_job(api, job_id):
    response = api.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def claim(api):
    response = api.post("/api/tasks/claim", json={"node_name": NODE, "vram_mb": 0,
                                               "capabilities": ["text_generation"], "supported_tasks": ["text"]})
    assert response.status_code == 200, response.text
    return response.json()["task"]


def result(api, task, *, success=True):
    artifacts = execute_role_task("text", task) if success else []
    response = api.post("/api/tasks/result", json={"node_name": NODE, "task_id": task["task_id"],
        "job_id": task["job_id"], "success": success, "artifacts": artifacts,
        "error": None if success else "controlled Text failure"})
    return response, artifacts


def consistent(contour):
    api = contour["api"]
    queue = api.get("/api/tasks/queue").json()
    dashboard = api.get("/api/status").json()["queue"]
    metrics = api.get("/api/metrics").json()
    assert dashboard["backend"] == "redis"
    for key, field, metric in (("ready", "depth", "queue_ready"), ("inflight", "inflight", "queue_inflight"),
                               ("dead_letter", "dead_letter", "queue_dead_letter")):
        assert queue["total"][key] == dashboard[field] == metrics[metric]
    jobs = api.get("/api/jobs").json()
    by_id = {job["job_id"]: job for job in jobs}
    for task in queue["ready"] + queue["inflight"]:
        assert task["job_status"] == by_id[task["job_id"]]["status"]
    return queue


def test_core_and_redis_restart_preserve_unique_text_tasks(contour):
    api = contour["api"]
    heartbeat(api, passed=False)
    job_id = create(api)
    assert claim(api) is None
    heartbeat(api)
    before = consistent(contour)
    issued = claim(api)
    assert issued and before["ready"][0]["task_id"] == issued["task_id"], (before, api.get("/api/workers").json())
    assert issued["character"]["voice"]["model"] == "process-model"
    assert consistent(contour)["total"] == {"ready": 0, "inflight": 1, "dead_letter": 0, "scheduled": 0}
    contour["restart"]("core")
    after = consistent(contour)
    assert after["total"]["ready"] == 1 and after["total"]["inflight"] == 0
    replacement_id = after["ready"][0]["task_id"]
    assert replacement_id != issued["task_id"]
    contour["restart"]("redis", crash=True)
    assert consistent(contour)["ready"][0]["task_id"] == replacement_id
    heartbeat(api)
    resumed = claim(api)
    assert resumed["task_id"] == replacement_id
    denied, _ = result(api, issued)
    assert denied.status_code == 409
    response, _ = result(api, resumed)
    assert response.status_code == 200, response.text
    assert get_job(api, job_id)["status"] == "SCRIPT_PENDING_APPROVAL"
    assert consistent(contour)["total"]["ready"] == consistent(contour)["total"]["inflight"] == 0
    harness.record_row("text_process_core_redis_restart", "SCRIPT_PENDING_APPROVAL", get_job(api, job_id)["status"],
        old_task_id=issued["task_id"], recovered_task_id=replacement_id,
        redis_version=contour["redis_identity"], redis_binary_sha256=contour["redis_binary_sha256"])


@pytest.mark.parametrize("contour", ["heartbeat", "lease"], indirect=True)
def test_storyboard_revision_retry_loss_restart_uses_current_default(contour):
    api = contour["api"]
    heartbeat(api)
    job_id = create(api)
    response, _ = result(api, claim(api))
    assert response.status_code == 200
    assert api.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "process"}).status_code == 200
    wait(lambda: get_job(api, job_id)["status"] == "STORYBOARD_QUEUED")
    heartbeat(api)
    task = claim(api)
    assert task["revision"] is None
    response, artifacts = result(api, task)
    assert response.status_code == 200, response.text
    board = api.get(f"/api/jobs/{job_id}/storyboards").json()[0]
    assert board["revision_request"] is None
    # Explicit revision, then a default history branch must not reuse it.
    response = api.post(f"/api/jobs/{job_id}/storyboards/regenerate", json={"version": 1, "revision": "current revision", "actor": "process"})
    assert response.status_code == 200, response.text
    heartbeat(api)
    current = claim(api)
    assert current["revision"] == "current revision"
    response, _ = result(api, current)
    assert response.status_code == 200
    response = api.post(f"/api/jobs/{job_id}/storyboards/regenerate", json={"version": 2, "revision": None, "actor": "process"})
    assert response.status_code == 200, response.text
    heartbeat(api)
    default = claim(api)
    assert default["revision"] is None
    response, _ = result(api, default, success=False)
    assert response.status_code == 200
    heartbeat(api)
    lost = claim(api)
    assert lost["revision"] is None
    wait(lambda: get_job(api, job_id)["status"] == "STORYBOARD_FAILED")
    queue = consistent(contour)
    assert queue["total"]["ready"] == queue["total"]["inflight"] == 0
    assert lost["task_id"] in {item["task_id"] for item in queue["dead_letter"]}
    persisted_worker = json.loads((contour["root"] / "jobs" / ".workers.json").read_text())[NODE]
    assert persisted_worker["current_task"] is None and persisted_worker["current_job"] is None
    attempts = [json.loads(line) for line in (contour["root"] / "jobs" / ".tasks.jsonl").read_text().splitlines()]
    assert [row for row in attempts if row["task"]["task_id"] == lost["task_id"]][-1]["status"] == "FAILED"
    contour["restart"]("core")
    assert get_job(api, job_id)["status"] == "STORYBOARD_FAILED"
    response = api.post(f"/api/jobs/{job_id}/storyboards/regenerate-from-history", json={"version": 2, "revision": None, "actor": "process"})
    assert response.status_code == 200, response.text
    heartbeat(api)
    pending = claim(api)
    assert pending["revision"] is None
    contour["restart"]("core")
    wait(lambda: get_job(api, job_id)["status"] == "STORYBOARD_QUEUED")
    heartbeat(api)
    recovered = claim(api)
    assert recovered["revision"] is None
    response, _ = result(api, recovered)
    assert response.status_code == 200, response.text
    boards = api.get(f"/api/jobs/{job_id}/storyboards").json()
    assert boards[-1]["revision_request"] is None
    assert contour["requests"] and all(body["model"] == "process-model" for body in contour["requests"])
    harness.record_row("text_process_default_revision_recovery_" + contour["loss"], None, boards[-1]["revision_request"],
        job_id=job_id, task_ids=[task["task_id"], current["task_id"], default["task_id"], lost["task_id"], recovered["task_id"]])


def test_text_cancel_fences_result_and_releases_queue(contour):
    api = contour["api"]
    heartbeat(api)
    job_id = create(api)
    issued = claim(api)
    assert issued
    renewed = api.post("/api/tasks/renew", json={"node_name": NODE, "task_id": issued["task_id"]})
    assert renewed.status_code == 200 and renewed.json()["renewed"] is True
    job = get_job(api, job_id)
    assert job["assigned_worker"] == NODE
    assert api.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    assert issued["task_id"] in {item["task_id"] for item in api.get(f"/api/tasks/cancellations/{NODE}").json()}
    response, _ = result(api, issued)
    assert response.status_code == 409
    heartbeat(api)
    assert claim(api) is None
    assert get_job(api, job_id)["status"] == "CANCELLED"
    assert consistent(contour)["total"]["ready"] == consistent(contour)["total"]["inflight"] == 0
    attempts = [json.loads(line) for line in (contour["root"] / "jobs" / ".tasks.jsonl").read_text().splitlines()]
    cancelled = [row for row in attempts if row["task"]["task_id"] == issued["task_id"]][-1]
    assert cancelled["status"] == "CANCELLED" and cancelled["node_name"] == NODE
    harness.record_row("text_process_cancel", "CANCELLED", cancelled["status"], job_id=job_id, task_id=issued["task_id"])


@pytest.mark.parametrize("contour", ["heartbeat", "lease"], indirect=True)
def test_script_loss_has_one_replacement_and_no_phantom_owner(contour):
    api = contour["api"]
    heartbeat(api)
    job_id = create(api)
    issued = claim(api)
    assert issued
    wait(lambda: get_job(api, job_id)["status"] == "SCRIPT_QUEUED")
    queue = consistent(contour)
    assert queue["total"]["ready"] == 1 and queue["total"]["inflight"] == 0
    replacement = queue["ready"][0]["task_id"]
    assert replacement != issued["task_id"]
    worker = json.loads((contour["root"] / "jobs" / ".workers.json").read_text())[NODE]
    assert worker["current_task"] is None and worker["current_job"] is None
    heartbeat(api)
    recovered = claim(api)
    assert recovered["task_id"] == replacement
    response, _ = result(api, recovered)
    assert response.status_code == 200, response.text
    assert get_job(api, job_id)["assigned_worker"] is None
    harness.record_row("text_process_script_recovery_" + contour["loss"], "SCRIPT_PENDING_APPROVAL",
        get_job(api, job_id)["status"], job_id=job_id, old_task_id=issued["task_id"], recovered_task_id=replacement)


@pytest.mark.parametrize("stage", ["script", "storyboard"])
def test_failed_text_results_exhaust_budget_without_duplicate_dead_letters(contour, stage):
    api = contour["api"]
    heartbeat(api)
    job_id = create(api)
    if stage == "storyboard":
        response, _ = result(api, claim(api))
        assert response.status_code == 200
        assert api.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "process"}).status_code == 200
        wait(lambda: get_job(api, job_id)["status"] == "STORYBOARD_QUEUED")
    ids = []
    for attempt in range(2):
        heartbeat(api)
        task = claim(api)
        assert task and task["task"] == stage
        ids.append(task["task_id"])
        response, _ = result(api, task, success=False)
        expected_code = 400 if stage == "storyboard" and attempt == 1 else 200
        assert response.status_code == expected_code, response.text
    expected = stage.upper() + "_FAILED"
    assert get_job(api, job_id)["status"] == expected
    queue = consistent(contour)
    assert queue["total"]["ready"] == queue["total"]["inflight"] == 0
    assert sorted(item["task_id"] for item in queue["dead_letter"]) == sorted(ids)
    worker = json.loads((contour["root"] / "jobs" / ".workers.json").read_text())[NODE]
    assert worker["current_task"] is None and worker["current_job"] is None
    harness.record_row("text_process_failed_results_" + stage, expected, get_job(api, job_id)["status"],
                       job_id=job_id, task_ids=ids)
