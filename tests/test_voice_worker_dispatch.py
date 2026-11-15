"""i.0.0.4.70 (#470): voice preview/synthesis run through Worker dispatch.

The CORE endpoint enqueues a job-less ``voice_preview`` task; a Voice Worker
claims it over HTTP, runs the regular ``execute_task`` → ``execute_role_task``
voice executor against a real HTTP TTS runtime stand-in and returns the audio
through ``/api/tasks/result``. CORE itself must never call the TTS runtime for
synthesis and has no fallback when no Voice Worker is ready.
"""

import base64
import io
import json
import math
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import worker.role_executor as role_executor
from core import voice_dispatch
from core.app import app
from core.state import store, task_queue
from worker import service as worker_service

TOKEN = "voice-dispatch-worker-token"
NODE = "voice-node"
HEADERS = {"x-vertep-token": TOKEN}


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(i / 7))) for i in range(4410)))
    return buf.getvalue()


AUDIO = _wav()


class _Runtime:
    """A real HTTP listener standing in for the TTS runtime."""

    def __init__(self):
        self.synthesis: list[dict] = []
        self.fail = False
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"voices": [{"id": "uk", "name": "Ukrainian"},
                                              {"id": "en-us", "name": "English"}],
                                   "speed_range": {"min": 80, "max": 450}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                runtime.synthesis.append(json.loads(self.rfile.read(length)))
                if runtime.fail:
                    self.send_response(500)
                    self.end_headers()
                    return
                body = json.dumps({"audio_base64": base64.b64encode(AUDIO).decode(),
                                   "mime_type": "audio/wav", "engine": "espeak-ng"}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def runtime(monkeypatch):
    server = _Runtime()
    monkeypatch.setenv("TTS_URL", server.url)
    monkeypatch.setenv("NODE_API_TOKEN", TOKEN)
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("NODE_ROLE", "voice")
    monkeypatch.delenv("REQUIRE_WORKER_SELF_TEST", raising=False)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("CORE must not call the TTS runtime for synthesis")

    # The legacy helper stays a Worker-side function; CORE must never reach it.
    monkeypatch.setattr(role_executor, "synthesize_voice", _forbidden)
    yield server
    server.close()


def _client() -> TestClient:
    return TestClient(app, headers=HEADERS)


def _heartbeat(client, node=NODE, **overrides):
    payload = {"node_name": node, "vram_mb": 0, "role": "voice", "supported_tasks": ["voice"],
               "capabilities": ["speech_synthesis"],
               "voice_catalog": {"voices": ["en-us", "uk"], "speed_range": {"min": 80, "max": 450}}}
    payload.update(overrides)
    response = client.post("/api/workers/heartbeat", json=payload)
    assert response.status_code == 200, response.text
    return response


class _Worker:
    """The Worker loop: claim over HTTP, execute like ``worker.service``, submit."""

    def __init__(self, client, node=NODE, *, execute=True):
        self.client = client
        self.node = node
        self.execute = execute
        self.claimed: list[dict] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="voice-worker", daemon=True)

    def run(self):
        while not self.stop.is_set():
            task = self.client.post("/api/tasks/claim", json={
                "node_name": self.node, "vram_mb": 0, "supported_tasks": ["voice"],
                "capabilities": ["speech_synthesis"]}).json().get("task")
            if not task:
                time.sleep(0.02)
                continue
            self.claimed.append(task)
            if not self.execute:
                continue
            worker_service.renew_active_task(self.client, "", self.node, task)
            result = worker_service.execute_task(None, task, self.node)
            worker_service.submit_result(self.client, "", result)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop.set()
        self.thread.join(timeout=5)


def _preview(client, **payload):
    return client.post("/api/models/voices/preview", json=payload)


def _no_preview_tasks():
    return not any(voice_dispatch.is_preview_task(task)
                   for task in task_queue.ready_tasks() + task_queue.inflight_tasks())


class TestCoreWorkerTtsAudio:
    def test_preview_runs_on_the_voice_worker(self, runtime):
        client = _client()
        _heartbeat(client)
        with _Worker(client) as worker:
            response = _preview(client, text="Привіт", voice="uk", speed=170)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.content == AUDIO
        [task] = worker.claimed
        assert task["kind"] == "voice_preview" and task["task"] == "voice"
        # The runtime was reached by the Worker, with the operator's parameters only.
        [request] = runtime.synthesis
        assert request == {
            "text": "Привіт", "voice": "uk", "speed": 170}
        assert store.workers[NODE]["current_task"] is None
        assert store.workers[NODE]["status"] == "READY"
        assert _no_preview_tasks()

    def test_default_voice_and_engine_default_speed(self, runtime):
        client = _client()
        _heartbeat(client)
        with _Worker(client):
            response = _preview(client, text="hi")
        assert response.status_code == 200
        assert runtime.synthesis[0]["voice"] == "default"
        assert runtime.synthesis[0]["speed"] == 150

    def test_synthesis_endpoint_keeps_language_and_model(self, runtime):
        client = _client()
        _heartbeat(client)
        with _Worker(client):
            response = client.post("/api/models/voices/synthesize", json={
                "text": "Hello", "voice": "en-us", "speed": 200, "language": "en", "model": "espeak-ng"})
        assert response.status_code == 200, response.text
        assert response.content == AUDIO
        request = runtime.synthesis[0]
        assert (request["voice"], request["speed"], request["language"], request["model"]) == (
            "en-us", 200, "en", "espeak-ng")


class TestNoCoreFallback:
    def test_without_a_voice_worker_preview_is_503(self, runtime):
        client = _client()
        response = _preview(client, text="hi", voice="uk")
        assert response.status_code == 503
        assert response.json()["code"] == "no_ready_voice_worker_is_available"
        assert runtime.synthesis == []
        assert _no_preview_tasks()

    def test_without_a_voice_worker_synthesis_is_503(self, runtime):
        response = _client().post("/api/models/voices/synthesize", json={"text": "hi"})
        assert response.status_code == 503
        assert runtime.synthesis == []

    def test_a_worker_without_speech_synthesis_is_not_ready(self, runtime):
        client = _client()
        _heartbeat(client, role="text", supported_tasks=["text"], capabilities=["text_generation"])
        assert _preview(client, text="hi").status_code == 503
        assert runtime.synthesis == []

    def test_a_worker_without_the_voice_is_not_ready(self, runtime):
        client = _client()
        _heartbeat(client, voice_catalog={"voices": ["en-us"]})
        assert _preview(client, text="hi", voice="uk").status_code == 503

    def test_stale_heartbeat_is_not_ready(self, runtime):
        client = _client()
        _heartbeat(client)
        store.workers[NODE]["last_seen"] = "2000-01-01T00:00:00+00:00"
        assert _preview(client, text="hi").status_code == 503


class TestWorkerErrors:
    def test_runtime_failure_on_the_worker_is_502(self, runtime):
        runtime.fail = True
        client = _client()
        _heartbeat(client)
        with _Worker(client):
            response = _preview(client, text="hi")
        assert response.status_code == 502
        assert response.json()["code"] == "voice_preview_failed"
        assert store.workers[NODE]["current_task"] is None

    def test_tampered_contract_is_502(self, runtime, monkeypatch):
        original = worker_service.execute_task

        def tampered(adapter, task, node):
            result = original(adapter, task, node)
            result["artifacts"][0]["contract"]["sha256"] = "0" * 64
            return result

        monkeypatch.setattr(worker_service, "execute_task", tampered)
        client = _client()
        _heartbeat(client)
        with _Worker(client):
            assert _preview(client, text="hi").status_code == 502

    def test_silent_worker_times_out_and_late_result_is_refused(self, runtime, monkeypatch):
        monkeypatch.setenv("VOICE_PREVIEW_TIMEOUT_SECONDS", "1")
        client = _client()
        _heartbeat(client)
        with _Worker(client, execute=False) as worker:
            response = _preview(client, text="hi")
        assert response.status_code == 504
        assert response.json()["code"] == "voice_worker_did_not_return_audio_in_time"
        [task] = worker.claimed
        assert {"task_id": task["task_id"]} in [
            {"task_id": item["task_id"]} for item in task_queue.pop_cancellations(NODE)]
        assert store.workers[NODE]["current_task"] is None
        assert _no_preview_tasks()
        late = client.post("/api/tasks/result", json={
            "job_id": task["job_id"], "task_id": task["task_id"], "node_name": NODE,
            "success": True, "artifacts": []})
        assert late.status_code == 409
        renew = client.post("/api/tasks/renew", json={"node_name": NODE, "task_id": task["task_id"]})
        assert renew.status_code == 409

    def test_foreign_node_result_is_refused(self, runtime):
        client = _client()
        _heartbeat(client)
        task = voice_dispatch.build_task("hi", "uk", 150)
        voice_dispatch._pending[task["task_id"]] = voice_dispatch._Pending(task)
        try:
            assert voice_dispatch.claimed(task["task_id"], NODE)
            response = client.post("/api/tasks/result", json={
                "job_id": task["job_id"], "task_id": task["task_id"], "node_name": "other-node",
                "success": True, "artifacts": []})
            assert response.status_code == 409
        finally:
            voice_dispatch._pending.pop(task["task_id"], None)

    def test_abandoned_preview_is_dropped_at_claim(self, runtime):
        client = _client()
        _heartbeat(client)
        task_queue.enqueue(voice_dispatch.build_task("hi", "uk", 150))
        answer = client.post("/api/tasks/claim", json={
            "node_name": NODE, "vram_mb": 0, "supported_tasks": ["voice"]}).json()
        assert answer["task"] is None
        assert _no_preview_tasks()


def test_core_models_api_no_longer_imports_synthesize_voice():
    import core.api.models as models_api
    assert not hasattr(models_api, "synthesize_voice")
