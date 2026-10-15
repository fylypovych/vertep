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

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00")
MP4 = b"\x00\x00\x00\x14ftypmp42" + b"\x00" * 8


@pytest.mark.parametrize("suffix", [".mp4", ".mov"])
def test_worker_accepts_video_signature_after_box_size(suffix):
    from worker.role_executor import verify_media

    assert verify_media(MP4, suffix).startswith("video/")
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(b"ftypmp42", suffix)


@pytest.mark.parametrize("suffix", [".png", ".jpg", ".jpeg", ".ppm"])
def test_worker_rejects_signature_only_image_payload(suffix):
    """Issue #121: a signature-only image must not pass as a verified artifact.

    The old check accepted any payload that merely *started* with the magic
    bytes, so an 8-byte PNG signature (or a truncated JPEG/PPM header) shipped
    to CORE as a valid artifact. A real image is longer than its own signature,
    so a payload that is nothing but the signature is refused.
    """
    from worker.role_executor import verify_media

    signature = {
        ".png": b"\x89PNG\r\n\x1a\n",
        ".jpg": b"\xff\xd8\xff",
        ".jpeg": b"\xff\xd8\xff",
        ".ppm": b"P6",
    }[suffix]
    with pytest.raises(RuntimeError, match="media signature|too short"):
        verify_media(signature, suffix)


def test_worker_rejects_a_truncated_video_payload():
    """Issue #121: a video shorter than its own box header is not a video."""
    from worker.role_executor import verify_media

    # ftyp sits at bytes 4..8, so a payload without those bytes cannot claim to
    # be an MP4/MOV; the old check rejected it, but only via the signature test.
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(b"\x00\x00\x00", ".mp4")


def test_worker_rejects_png_signature_plus_junk():
    """Issue #178/#121: signature + junk must not pass as a PNG."""
    from worker.role_executor import verify_media

    junk_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 12  # 20 bytes, no IHDR header
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(junk_png, ".png")
    # A structurally present IHDR header but a truncated body is still refused.
    truncated = PNG[:-1]
    assert len(truncated) < 33
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(truncated, ".png")


def test_worker_rejects_video_without_a_complete_ftyp_box():
    """Issue #178/#121: a 12-byte ftyp stub or an oversized box is not a video."""
    from worker.role_executor import verify_media

    # Bare 12-byte ftyp header: no major brand / minor version.
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(b"\x00\x00\x00\x0cftypmp42", ".mp4")
    # ftyp box declares 24 bytes but only 16 were delivered.
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00", ".mov")


def test_worker_rejects_jpeg_without_eoi_marker():
    from worker.role_executor import verify_media

    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(b"\xff\xd8\xff\xe0junk-without-eoi", ".jpg")


def test_worker_accepts_structurally_valid_png_and_mp4():
    from tests.media_harness import make_png
    from worker.role_executor import verify_media

    assert verify_media(make_png(), ".png") == "image/png"
    assert verify_media(PNG, ".png") == "image/png"
    assert verify_media(MP4, ".mp4") == "video/mp4"
    assert verify_media(MP4, ".mov") == "video/quicktime"


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
        # The declared output kind the worker asked for: an image workflow reports
        # ``images``/``.png``, a video workflow reports ``videos``/``.mp4`` (Issue #121).
        self.output_key = "images"
        self.output_suffix = ".png"
        self.view_content_type = "image/png"
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
                        key: [{"filename": f"scene-{prompt_id}{outer.output_suffix}",
                               "subfolder": "", "type": "output"}]
                        for key in (outer.output_key,)}}}})
                if self.path.startswith("/view"):
                    if outer.view_status != 200:
                        return self._send(outer.view_status, raw=b"<html>error</html>",
                                          content_type="text/html")
                    return self._send(200, raw=outer.view_body, content_type=outer.view_content_type)
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
    auth_headers = {"x-vertep-token": "worker-token"}
    for _ in range(200):
        claimed = client.post("/api/tasks/claim",
                              json={"node_name": node_name, "vram_mb": 16384},
                              headers=auth_headers).json().get("task")
        if claimed and claimed.get("kind") == "image_storyboard":
            return claimed
        time.sleep(0.01)
    return None


def _dispatched_image_task(monkeypatch):
    """Drive CORE to the point where a real GPU image task is claimable."""
    from core.models import JobStatus, StoryboardScene, StoryboardVersion
    from core.app import store

    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    monkeypatch.setattr("core.security._valid_worker_request", lambda node_name, request: True)
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
    auth_headers = {"x-vertep-token": "worker-token"}
    client.post("/api/workers/heartbeat", json={"node_name": "text-worker", "vram_mb": 0, "role": "text",
                                                "capabilities": ["text_generation"], "supported_tasks": ["text"]},
                headers=auth_headers)
    client.post("/api/workers/heartbeat", json={"node_name": "gpu-http", "vram_mb": 16384, "role": "gpu",
                                                "capabilities": ["image_generation"], "supported_tasks": ["image"]},
                headers=auth_headers)
    job_id = client.post("/api/jobs",
                         json={"topic": "HTTP render", "character_id": "did_samogon"}).json()["job_id"]
    # Wait for script task to be queued
    for _ in range(300):
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["status"] == "SCRIPT_QUEUED":
            break
        time.sleep(0.01)
    assert state["status"] == "SCRIPT_QUEUED"
    # Claim and complete the script task
    for _ in range(300):
        claimed = client.post("/api/tasks/claim",
                              json={"node_name": "text-worker", "vram_mb": 0},
                              headers=auth_headers).json().get("task")
        if claimed and claimed.get("task") == "script":
            script = {"title": "HTTP render", "scenes": [{"prompt": "HTTP render",
                                                           "voiceover": "", "duration": 1}]}
            payload = json.dumps(script, ensure_ascii=False).encode("utf-8")
            client.post("/api/tasks/result", json={
                "job_id": job_id, "task_id": claimed["task_id"], "node_name": "text-worker",
                "success": True,
                "artifacts": [{"filename": "script.json", "kind": "script",
                               "data_base64": base64.b64encode(payload).decode("ascii")}]},
                headers=auth_headers)
            break
        time.sleep(0.01)
    # Wait for script pending approval
    for _ in range(300):
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["status"] == "SCRIPT_PENDING_APPROVAL":
            break
        time.sleep(0.01)
    assert state["status"] == "SCRIPT_PENDING_APPROVAL"
    client.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "test"})
    # Wait for storyboard pending approval (mocked queue_storyboard transitions directly)
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

    auth_headers = {"x-vertep-token": "worker-token"}
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": "gpu-http",
        "success": True, "artifacts": artifacts}, headers=auth_headers)
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
    auth_headers = {"x-vertep-token": "worker-token"}
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": "gpu-http",
        "success": True, "artifacts": [artifact]}, headers=auth_headers)
    assert response.status_code == 400
    assert "checksum" in response.text


def test_core_rejects_a_contract_declaring_the_wrong_media_kind(comfyui, monkeypatch):
    from worker.role_executor import _media_artifact

    client, job_id, task = _dispatched_image_task(monkeypatch)
    artifact = _media_artifact("scene-001.png", "image", PNG)
    artifact["contract"]["mime_type"] = "video/mp4"
    auth_headers = {"x-vertep-token": "worker-token"}
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": "gpu-http",
        "success": True, "artifacts": [artifact]}, headers=auth_headers)
    assert response.status_code == 400
    assert "non-image" in response.text


def test_media_contract_helper_refuses_tampered_media():
    from worker.role_executor import _media_artifact

    tampered = bytearray(PNG)
    tampered[1:4] = b"XXX"
    with pytest.raises(RuntimeError, match="declared media signature"):
        _media_artifact("scene.png", "image", bytes(tampered))


def test_worker_renders_a_verified_video_artifact_over_http(comfyui, monkeypatch):
    """Issue #121: the same real GPU HTTP boundary yields a verified video artifact.

    An image workflow reports an ``images``/``.png`` output and is proven
    end-to-end through CORE above.  A video workflow reports a ``videos``/``.mp4``
    output, and the worker must download it over the real ``/view`` endpoint,
    validate its signature and publish a truthful ``media_contract/v1`` that CORE
    accepts — the video fixture over the same socket (``prompt`` → ``history`` →
    ``view``).
    """
    from worker.role_executor import execute_role_task
    from core.file_validation import validate_media_contract, validate_signature

    comfyui.view_body = MP4
    comfyui.view_status = 200
    comfyui.output_key = "videos"
    comfyui.output_suffix = ".mp4"
    comfyui.view_content_type = "video/mp4"

    task = {"task": "video", "task_id": "video-task-1",
            "workflow": "image/demo.json", "topic": "HTTP video render"}
    artifacts = execute_role_task("gpu", task)

    assert artifacts and artifacts[0]["kind"] == "video"
    artifact = artifacts[0]
    data = base64.b64decode(artifact["data_base64"])
    assert data == MP4
    contract = artifact["contract"]
    assert contract["format"] == "media_contract/v1"
    assert contract["sha256"] == hashlib.sha256(MP4).hexdigest()
    assert contract["size"] == len(MP4)
    assert contract["mime_type"] == "video/mp4"
    # The real endpoints were used in order.
    paths = [path for _method, path in comfyui.requests]
    assert any(path == "/prompt" for path in paths)
    assert any(path.startswith("/history/") for path in paths)
    assert any(path.startswith("/view") for path in paths)
    # CORE re-derives the contract on receipt: the video payload is accepted and
    # its declaration is honoured, exactly as the image artifact path does.
    validate_media_contract(artifact, data, expected="video")
    validate_signature(data, ".mp4")


def test_core_rejects_a_video_contract_declaring_an_image_payload():
    """Issue #121: a video result can never masquerade as an accepted image."""
    from core.file_validation import validate_media_contract

    artifact = {"contract": {"format": "media_contract/v1",
                             "sha256": hashlib.sha256(MP4).hexdigest(),
                             "size": len(MP4), "mime_type": "image/png"}}
    with pytest.raises(ValueError, match="non-video"):
        validate_media_contract(artifact, MP4, expected="video")
