"""Issue #83: controlled HTTP CORE → GPU Worker → prompt/history/view → artifact.

The GPU path is exercised over a real socket (no stubbed ``httpx``): a local
ComfyUI server implements ``/prompt``, ``/history/{id}``, ``/view`` and
``DELETE /queue``, the real worker executor renders through the real
``ComfyUIAdapter``, and the real CORE API accepts and verifies the resulting
artifact.  Replacing only the orchestration would not prove that the transport,
the media integrity checks and the cancellation isolation actually work.

Negative paths covered: malformed history payloads, an HTML error body served
as an image, a timeout, cancellation isolation and late-result rejection.
"""

import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from adapters.comfyui import ComfyUIAdapter, PromptCancelledError

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 9)
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8


@pytest.mark.parametrize("suffix", [".mp4", ".mov"])
def test_worker_accepts_video_signature_after_box_size(suffix):
    from worker.role_executor import verify_media

    assert verify_media(MP4, suffix).startswith("video/")
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(b"ftypmp42", suffix)


class FakeComfyUI:
    """Minimal ComfyUI HTTP surface over a real socket."""

    def __init__(self):
        self.prompts = {}
        self.removed = []
        self.interrupts = 0
        self.requests = []
        self.view_body = PNG
        self.view_status = 200
        self.history_override = None
        self.never_finishes = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status, payload=None, raw=None, content_type="application/json"):
                body = raw if raw is not None else json.dumps(payload or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                outer.requests.append(("POST", self.path))
                if self.path == "/interrupt":
                    outer.interrupts += 1
                    return self._send(200, {})
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                prompt_id = f"prompt-{len(outer.prompts) + 1}"
                outer.prompts[prompt_id] = payload.get("prompt", {})
                return self._send(200, {"prompt_id": prompt_id})

            def do_DELETE(self):
                outer.requests.append(("DELETE", self.path))
                if self.path.startswith("/queue"):
                    from urllib.parse import parse_qs, urlparse
                    prompt_id = (parse_qs(urlparse(self.path).query).get("prompt_id") or [None])[0]
                    if prompt_id in outer.prompts and not outer.never_finishes:
                        del outer.prompts[prompt_id]
                        outer.removed.append(prompt_id)
                        return self._send(200, {})
                    return self._send(400, {"error": "prompt is executing"})
                return self._send(404, {})

            def do_GET(self):
                outer.requests.append(("GET", self.path))
                if self.path.startswith("/history/"):
                    prompt_id = self.path.rsplit("/", 1)[-1]
                    if outer.history_override is not None:
                        return self._send(200, outer.history_override)
                    if outer.never_finishes or prompt_id not in outer.prompts:
                        return self._send(200, {})
                    return self._send(200, {prompt_id: {"outputs": {"9": {
                        key: [{"filename": f"scene-{prompt_id}.png", "subfolder": "", "type": "output"}]
                        for key in ("images",)}}}})
                if self.path.startswith("/view"):
                    if outer.view_status != 200:
                        return self._send(outer.view_status, raw=b"<html>error</html>",
                                          content_type="text/html")
                    return self._send(200, raw=outer.view_body, content_type="image/png")
                if self.path == "/system_stats":
                    return self._send(200, {"system": {"comfyui_version": "0.34.0"}})
                return self._send(404, {})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def comfyui(monkeypatch, tmp_path):
    from adapters.providers import DefaultComputeProvider, get_providers

    server = FakeComfyUI()
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("COMFYUI_URL", server.url)
    monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path))
    # Use the real adapter for this server, independent of prior registry swaps.
    monkeypatch.setitem(get_providers()._providers, "compute",
                        DefaultComputeProvider(ComfyUIAdapter()))
    workflow = tmp_path / "image" / "demo.json"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(json.dumps({"1": {"class_type": "CLIPTextEncode",
                                          "inputs": {"text": "{{TOPIC}}"}}}), encoding="utf-8")
    yield server
    server.close()


# ── adapter over real HTTP ──────────────────────────────────────────────────


def test_prompt_history_view_round_trip_over_http(comfyui, tmp_path):
    adapter = ComfyUIAdapter()
    data, filename, kind = adapter.generate_output("image/demo.json", "котики 🐈", "image")
    assert kind == "image"
    assert data == PNG
    assert filename.startswith("scene-prompt-")
    # The real endpoints were used, in order.
    paths = [path for _method, path in comfyui.requests]
    assert any(path == "/prompt" for path in paths)
    assert any(path.startswith("/history/") for path in paths)
    assert any(path.startswith("/view") for path in paths)


def test_topic_reaches_comfyui_verbatim_over_http(comfyui, tmp_path):
    adapter = ComfyUIAdapter()
    topic = 'рядок\n"лапки" \\ C:\\шлях 🌍'
    adapter.generate_output("image/demo.json", topic, "image")
    submitted = list(comfyui.prompts.values())[0]
    assert submitted["1"]["inputs"]["text"] == topic


def test_html_error_body_is_not_accepted_as_an_image(comfyui):
    comfyui.view_status = 500
    adapter = ComfyUIAdapter()
    with pytest.raises(Exception) as error:
        adapter.generate_output("image/demo.json", "x", "image")
    assert "500" in str(error.value)


def test_truncated_media_is_rejected_by_the_worker_contract(comfyui, monkeypatch):
    """A non-media body must never become an artifact with a valid contract."""
    from worker.role_executor import verify_media

    comfyui.view_body = b"<html>gateway timeout</html>"
    adapter = ComfyUIAdapter()
    data, _filename, _kind = adapter.generate_output("image/demo.json", "x", "image")
    with pytest.raises(RuntimeError, match="does not match its declared media signature"):
        verify_media(data, ".png")


def test_timeout_is_reported_as_a_timeout(comfyui, monkeypatch):
    comfyui.never_finishes = True
    monkeypatch.setattr("adapters.comfyui.time.sleep", lambda _seconds: None)
    adapter = ComfyUIAdapter()
    with pytest.raises(TimeoutError):
        adapter.wait_for_result("prompt-1", timeout=1)


def test_malformed_history_is_reported_as_missing_output(comfyui):
    comfyui.history_override = {"prompt-1": {"outputs": {"9": {"unexpected": []}}}}
    adapter = ComfyUIAdapter()
    with pytest.raises(RuntimeError, match="without a image output"):
        adapter.generate_output("image/demo.json", "x", "image")


# ── cancellation isolation over real HTTP ───────────────────────────────────


def test_cancel_never_calls_the_global_interrupt_endpoint(comfyui):
    """Two jobs share one ComfyUI; cancelling one must not abort the other."""
    adapter = ComfyUIAdapter()
    mine = ComfyUIAdapter()
    mine.submit({"1": {}})                      # another job's prompt
    adapter.submit({"1": {}})
    adapter.current_prompt_id = next(iter(comfyui.prompts))
    # The prompt is already executing, so it cannot be removed one by one.
    comfyui.never_finishes = True
    assert adapter.cancel() is False
    assert comfyui.interrupts == 0
    assert ("POST", "/interrupt") not in comfyui.requests
    # The other job's prompt is untouched and still running.
    assert len(comfyui.prompts) == 2


def test_queued_prompt_is_removed_without_touching_others(comfyui):
    adapter = ComfyUIAdapter()
    ComfyUIAdapter().submit({"1": {}})
    adapter.submit({"1": {}})
    adapter.current_prompt_id = next(iter(comfyui.prompts))
    assert adapter.cancel() is True
    assert comfyui.interrupts == 0
    assert len(comfyui.prompts) == 1


def test_late_result_after_cancellation_is_rejected(comfyui, monkeypatch):
    comfyui.never_finishes = True
    monkeypatch.setattr("adapters.comfyui.time.sleep", lambda _seconds: None)
    adapter = ComfyUIAdapter()
    adapter.submit({"1": {}})
    adapter.current_prompt_id = next(iter(comfyui.prompts))
    assert adapter.cancel() is False
    # The prompt finishes anyway; its result must be discarded, not returned.
    comfyui.never_finishes = False
    with pytest.raises(PromptCancelledError):
        adapter.wait_for_result(next(iter(comfyui.prompts)), timeout=5)


# ── CORE → Worker → artifact over real HTTP ─────────────────────────────────


def _core_client():
    from core.app import app
    return TestClient(app)


def _claim_image_storyboard_task(client, node_name="gpu-http"):
    for _ in range(200):
        claimed = client.post("/api/tasks/claim",
                              json={"node_name": node_name, "vram_mb": 16384}).json().get("task")
        if claimed and claimed.get("kind") == "image_storyboard":
            return claimed
        time.sleep(0.01)
    return None


def _dispatched_image_task(monkeypatch):
    """Drive CORE to the point where a real GPU image task is claimable."""
    from core.script_agent import ScriptAgent
    from core.models import JobStatus, StoryboardScene, StoryboardVersion
    from core.app import store

    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client = _core_client()

    def _queue_storyboard(self, job, revision=None):
        script = job.script or {"title": job.topic,
                                "scenes": [{"prompt": job.topic, "voiceover": "", "duration": 1}]}
        scenes = [StoryboardScene(index=index, prompt=item.get("prompt", ""),
                                  video_prompt=item.get("prompt", ""),
                                  voiceover=item.get("voiceover", ""),
                                  duration=float(item.get("duration", 1)))
                  for index, item in enumerate(script.get("scenes", []), 1)]
        version = (job.storyboards[-1].version + 1 if job.storyboards else 1)
        board = StoryboardVersion(version=version, title=script.get("title", job.topic),
                                  description=script.get("description", ""),
                                  hashtags=script.get("hashtags", []), scenes=scenes,
                                  status="pending_approval", image_status="pending", image_version=1)
        for scene in board.scenes:
            scene.scene_id = f"sb-{board.version}-{scene.index}"
            scene.image_prompt = scene.prompt
            scene.image_version = 1
        job.storyboards.append(board)
        job.active_storyboard_version = board.version
        job.active_image_version = board.image_version
        job.storyboard_task_id = "mock-storyboard"
        store.update(job, JobStatus.STORYBOARD_PENDING_APPROVAL, "STORYBOARD PENDING APPROVAL")
        from core.image_storyboard import queue_image_storyboard
        queue_image_storyboard(store, job, board.version)
        return board

    monkeypatch.setattr("core.storyboard.StoryboardService.queue", _queue_storyboard)
    monkeypatch.setattr(ScriptAgent, "generate_script", lambda self, topic, system_prompt="", character=None: {
        "title": topic, "scenes": [{"prompt": topic, "voiceover": "", "duration": 1}]})
    client.post("/api/workers/heartbeat", json={"node_name": "text-worker", "vram_mb": 0, "role": "text",
                                                "capabilities": ["text_generation"], "supported_tasks": ["text"]})
    client.post("/api/workers/heartbeat", json={"node_name": "gpu-http", "vram_mb": 16384, "role": "gpu",
                                                "capabilities": ["image_generation"], "supported_tasks": ["image"]})
    job_id = client.post("/api/jobs",
                         json={"topic": "HTTP render", "character_id": "did_samogon"}).json()["job_id"]
    for _ in range(300):
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["status"] == "SCRIPT_PENDING_APPROVAL":
            break
        claimed = client.post("/api/tasks/claim",
                              json={"node_name": "text-worker", "vram_mb": 0}).json().get("task")
        if claimed and claimed.get("task") == "script":
            # The GPU transport is what this test proves; the text side is fed a
            # ready script so the pipeline reaches the image stage.
            script = {"title": "HTTP render", "scenes": [{"prompt": "HTTP render",
                                                           "voiceover": "", "duration": 1}]}
            payload = json.dumps(script, ensure_ascii=False).encode("utf-8")
            client.post("/api/tasks/result", json={
                "job_id": job_id, "task_id": claimed["task_id"], "node_name": "text-worker",
                "success": True,
                "artifacts": [{"filename": "script.json", "kind": "script",
                               "data_base64": base64.b64encode(payload).decode("ascii")}]})
        time.sleep(0.01)
    assert state["status"] == "SCRIPT_PENDING_APPROVAL"
    client.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "test"})
    for _ in range(300):
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["status"] == "STORYBOARD_PENDING_APPROVAL":
            break
        time.sleep(0.01)
    assert state["status"] == "STORYBOARD_PENDING_APPROVAL"
    task = _claim_image_storyboard_task(client)
    assert task is not None, "no image_storyboard task was dispatched"
    return client, job_id, task


def test_core_accepts_a_verified_image_artifact_from_a_real_http_render(comfyui, monkeypatch):
    """CORE → GPU Worker → prompt/history/view → verified image artifact."""
    from worker import role_executor

    client, job_id, task = _dispatched_image_task(monkeypatch)
    artifacts = role_executor.execute_role_task("gpu", task)
    assert artifacts and artifacts[0]["kind"] == "image"
    contract = artifacts[0]["contract"]
    assert contract["format"] == "media_contract/v1"
    assert contract["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert contract["size"] == len(PNG)
    assert contract["mime_type"] == "image/png"
    assert base64.b64decode(artifacts[0]["data_base64"]) == PNG

    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": "gpu-http",
        "success": True, "artifacts": artifacts})
    assert response.status_code == 200, response.text
    board = client.get(f"/api/jobs/{job_id}/storyboards").json()[0]
    for scene in board["scenes"]:
        assert scene.get("image_artifact_id"), scene
    # The render really travelled over HTTP and no global cancel happened.
    assert any(path.startswith("/history/") for _method, path in comfyui.requests)
    assert comfyui.interrupts == 0


def test_core_rejects_a_tampered_artifact_contract(comfyui, monkeypatch):
    """A contract that does not match the bytes must be refused by CORE."""
    from worker.role_executor import _media_artifact

    client, job_id, task = _dispatched_image_task(monkeypatch)
    artifact = _media_artifact("scene-001.png", "image", PNG)
    artifact["contract"]["sha256"] = "0" * 64
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": "gpu-http",
        "success": True, "artifacts": [artifact]})
    assert response.status_code == 400
    assert "checksum" in response.text


def test_core_rejects_a_contract_declaring_the_wrong_media_kind(comfyui, monkeypatch):
    from worker.role_executor import _media_artifact

    client, job_id, task = _dispatched_image_task(monkeypatch)
    artifact = _media_artifact("scene-001.png", "image", PNG)
    artifact["contract"]["mime_type"] = "video/mp4"
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": "gpu-http",
        "success": True, "artifacts": [artifact]})
    assert response.status_code == 400
    assert "non-image" in response.text


def test_media_contract_helper_refuses_tampered_media():
    from worker.role_executor import _media_artifact

    tampered = bytearray(PNG)
    tampered[1:4] = b"XXX"
    with pytest.raises(RuntimeError, match="declared media signature"):
        _media_artifact("scene.png", "image", bytes(tampered))
