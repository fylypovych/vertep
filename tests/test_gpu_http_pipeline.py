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
import struct
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from adapters.comfyui import ComfyUIAdapter, PromptCancelledError

# Proper decodable media fixtures (Issue #178.3/#178.4): the real MP4 fixture
# from test_gpu_media_fixtures carries ftyp + moov + mdat (media samples), not
# just ftyp + moov.  The stub MP4 below is kept for structural box-walk checks
# only; the end-to-end pipeline tests use MP4_VALID.
from tests.test_gpu_media_fixtures import MP4_VALID


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + chunk_type + payload
            + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF))


# A real, structurally valid PNG (correct IHDR dimensions/fields, matching
# chunk CRCs, a decompressable IDAT) and a real minimal MP4 (ftyp + moov with
# a video track): both satisfy the worker's strict box/chunk walk (Issue #178.3).
def _box(box_type: bytes, payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + box_type + payload


PNG = (b"\x89PNG\r\n\x1a\n"
       + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
       + _png_chunk(b"IDAT", zlib.compress(
           b"".join(b"\x00" + bytes((30, 40, 50)) * 2 for _ in range(2))))
       + _png_chunk(b"IEND", b""))
HDLR = _box(b"hdlr", b"\x00" * 8 + b"vide" + b"\x00" * 12 + b"\x00")
MOOV = _box(b"moov", _box(b"trak", _box(b"mdia", HDLR)))
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + MOOV


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
    assert len(truncated) >= 33
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(truncated, ".png")


def test_worker_rejects_an_ihdr_only_png_stub():
    """Issue #178.3: signature + IHDR without IDAT/IEND is a stub, not an image."""
    from worker.role_executor import verify_media

    ihdr_only = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
                 + b"\x00" * 13 + b"\x00\x00\x00\x00")
    assert len(ihdr_only) == 33
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(ihdr_only, ".png")


def test_worker_rejects_a_png_missing_iend():
    """Issue #178.3: a PNG cut after IDAT (no IEND) is truncated."""
    from worker.role_executor import verify_media

    without_iend = PNG[:PNG.rfind(b"IEND") - 4]
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(without_iend, ".png")


def test_worker_rejects_an_ftyp_only_mp4_stub():
    """Issue #178.3: an ``ftyp`` box without a following box is not a video."""
    from worker.role_executor import verify_media

    ftyp_only = b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
    assert len(ftyp_only) == 16
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(ftyp_only, ".mp4")
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(ftyp_only, ".mov")


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


def test_worker_rejects_png_with_invalid_dimensions():
    """Issue #178.3: a PNG whose IHDR declares a zero dimension is not an image.

    The #178 audit probe (a 61-byte PNG with invalid dimensions) must be
    refused even when every chunk CRC is internally consistent.
    """
    from worker.role_executor import verify_media

    zero_dimension = (b"\x89PNG\r\n\x1a\n"
                      + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 0, 0, 8, 2, 0, 0, 0))
                      + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
                      + _png_chunk(b"IEND", b""))
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(zero_dimension, ".png")


def test_worker_rejects_png_with_a_broken_chunk_crc():
    """Issue #178.3: corrupted bytes are caught by the per-chunk CRC, not only
    by a checksum of the whole payload."""
    from worker.role_executor import verify_media

    broken = bytearray(PNG)
    idat = broken.find(b"IDAT")
    assert idat > 0
    broken[idat + 4] ^= 0xFF  # flip a byte inside the IDAT payload; CRC now lies
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(bytes(broken), ".png")


def test_worker_rejects_png_with_junk_idat():
    """Issue #178.3: an IDAT payload that does not decompress is junk, even with
    a correct chunk CRC."""
    from worker.role_executor import verify_media

    junk_idat = (b"\x89PNG\r\n\x1a\n"
                 + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                 + _png_chunk(b"IDAT", b"not-a-zlib-stream")
                 + _png_chunk(b"IEND", b""))
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(junk_idat, ".png")


def test_worker_rejects_ftyp_plus_free_without_a_video_track():
    """Issue #178.3: ``ftyp`` + ``free`` (the audit's 24-byte probe) has no
    movie header and no video track — it is signature+junk, not a video."""
    from worker.role_executor import verify_media

    probe = (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
             + _box(b"free", b"\x00" * 8))
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(probe, ".mp4")
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(probe, ".mov")


def test_worker_rejects_moov_without_a_video_handler():
    """Issue #178.3: a ``moov`` box that carries no ``trak``/video ``hdlr`` is
    a movie header in name only and must not pass as a video."""
    from worker.role_executor import verify_media

    audio_only = (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
                  + _box(b"moov", b"\x00" * 16))
    with pytest.raises(RuntimeError, match="media signature"):
        verify_media(audio_only, ".mp4")


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
    # A video workflow resolves to ``workflows/video/<name>.json`` inside
    # WORKFLOWS_ROOT, exactly as the repository convention requires.
    video_workflow = tmp_path / "video" / "demo.json"
    video_workflow.parent.mkdir(parents=True, exist_ok=True)
    video_workflow.write_text(json.dumps({"1": {"class_type": "VHS_VideoCombine",
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

    comfyui.view_body = MP4_VALID
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
    assert data == MP4_VALID
    contract = artifact["contract"]
    assert contract["format"] == "media_contract/v1"
    assert contract["sha256"] == hashlib.sha256(MP4_VALID).hexdigest()
    assert contract["size"] == len(MP4_VALID)
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


# ── Issue #178.3: CORE re-verifies the media it stores ─────────────────────

#: The exact probes of the #212 audit: a PNG that carries only its IHDR chunk,
#: a MP4 that carries only an ftyp box and a MP4 whose ftyp is followed by a
#: free box instead of a movie header.
IHDR_ONLY_PNG = (b"\x89PNG\r\n\x1a\n"
                 + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)))
FTYP_ONLY_MP4 = b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
FTYP_PLUS_FREE_MP4 = (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
                      + _box(b"free", b"\x00" * 8))
TRUNCATED_PNG = PNG[:PNG.rfind(b"IEND") - 4]


@pytest.mark.parametrize("payload,suffix", [
    (IHDR_ONLY_PNG, ".png"),
    (TRUNCATED_PNG, ".png"),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 12, ".png"),
    (FTYP_ONLY_MP4, ".mp4"),
    (FTYP_PLUS_FREE_MP4, ".mp4"),
    (FTYP_ONLY_MP4, ".mov"),
])
def test_core_refuses_media_it_cannot_decode(payload, suffix):
    """CORE must not store a payload that only walks past the magic bytes."""
    from core.file_validation import validate_media_integrity

    with pytest.raises(ValueError, match="media signature"):
        validate_media_integrity(payload, suffix)


def test_core_accepts_structurally_valid_media():
    from core.file_validation import validate_media_integrity

    validate_media_integrity(PNG, ".png")
    validate_media_integrity(MP4, ".mp4")


def test_core_refuses_a_contract_whose_checksum_matches_damaged_bytes():
    """A truthful checksum is not enough: the payload must also be a real media."""
    from core.file_validation import validate_media_contract

    damaged = bytearray(PNG)
    damaged[1:4] = b"XXX"
    payload = bytes(damaged)
    artifact = {"contract": {"format": "media_contract/v1",
                             "sha256": hashlib.sha256(payload).hexdigest(),
                             "size": len(payload), "mime_type": "image/png"}}
    with pytest.raises(ValueError, match="media signature"):
        validate_media_contract(artifact, payload, expected="image")


def test_core_refuses_a_stub_payload_with_a_truthful_contract(comfyui, monkeypatch):
    """The refused payload must not reach the Job storage at all."""
    client, job, task, node_name, _auth = _dispatched_video_task(monkeypatch)
    artifact = {"filename": "scene-001.mp4", "kind": "video",
                "data_base64": base64.b64encode(FTYP_ONLY_MP4).decode("ascii"),
                "contract": {"format": "media_contract/v1", "kind": "video",
                             "mime_type": "video/mp4", "size": len(FTYP_ONLY_MP4),
                             "sha256": hashlib.sha256(FTYP_ONLY_MP4).hexdigest()}}
    response = _post_result(client, job, task, node_name=node_name, artifacts=[artifact])
    assert response.status_code == 400, response.text
    assert "media signature" in response.text
    assert not [item for item in job.artifacts if item.kind == "video_scene"]


# ── Issue #178.4: the video contour CORE → Worker → CORE ───────────────────

VIDEO_WORKFLOW = "workflows/video/demo.json"


def _unique_node(prefix="gpu-video"):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def _register_gpu(client, node_name, *, vram_mb=32768, auth=None):
    auth = auth or {"x-vertep-token": "worker-token"}
    client.post("/api/workers/heartbeat", json={
        "node_name": node_name, "vram_mb": vram_mb, "role": "gpu",
        "capabilities": ["video_generation", "image_generation"],
        "supported_tasks": ["video", "image"]}, headers=auth)
    return auth


def _video_job(monkeypatch, *, min_vram_mb=16384, scenes=1, topic="HTTP video render"):
    """Create a real video Job in ASSET_GENERATION with queued scene tasks."""
    from core.api.job_helpers import _enqueue_job_task
    from core.app import store
    from core.models import JobStatus, SceneRecord
    from core.orchestration import StageName, StageStatus, transition_stage

    job = store.create(topic, "did_samogon", 5, task_type="video",
                       min_vram_mb=min_vram_mb, workflow=VIDEO_WORKFLOW)
    for index in range(1, scenes + 1):
        job.scenes.append(SceneRecord(scene_id=f"scene-{index:03d}", index=index,
                                      prompt=f"{topic} {index}",
                                      video_prompt=f"{topic} {index}",
                                      voiceover="", duration=1))
    transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
    store.update(job, JobStatus.ASSET_GENERATION, "ASSET GENERATION STARTED")
    for scene in job.scenes:
        _enqueue_job_task(job, scene)
    return job


def _dispatched_video_task(monkeypatch, *, node_name=None, vram_mb=32768,
                            min_vram_mb=16384, scenes=1):
    """Drive CORE to the point where a real GPU video task is claimable."""
    node_name = node_name or _unique_node()
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    monkeypatch.setattr("core.security._valid_worker_request", lambda node, request: True)
    # The final assembly is not part of this contour: it would run FFmpeg in a
    # background thread, so it is observed but not executed here.
    monkeypatch.setattr("core.api.tasks._finalize_and_notify", lambda job, images: None)
    client = _core_client()
    auth = _register_gpu(client, node_name, vram_mb=vram_mb)
    job = _video_job(monkeypatch, min_vram_mb=min_vram_mb, scenes=scenes)
    for _ in range(200):
        claimed = client.post("/api/tasks/claim",
                              json={"node_name": node_name, "vram_mb": vram_mb},
                              headers=auth).json().get("task")
        if claimed and claimed.get("job_id") == job.job_id and claimed.get("task") == "video":
            return client, job, claimed, node_name, auth
        time.sleep(0.01)
    raise AssertionError("no video task was dispatched")


def _post_result(client, job, task, *, node_name, success=True, artifacts=None, error=None):
    payload = {"job_id": job.job_id, "task_id": task["task_id"], "node_name": node_name,
               "success": success, "artifacts": artifacts or []}
    if error is not None:
        payload["error"] = error
    return client.post("/api/tasks/result", json=payload,
                       headers={"x-vertep-token": "worker-token"})


def _video_runtime(comfyui):
    """Make the fake ComfyUI answer with a real MP4 render."""
    comfyui.view_body = MP4_VALID
    comfyui.view_status = 200
    comfyui.output_key = "videos"
    comfyui.output_suffix = ".mp4"
    comfyui.view_content_type = "video/mp4"


def test_core_imports_a_verified_video_scene_from_a_real_http_render(comfyui, monkeypatch):
    """Issue #178.4: CORE → GPU Worker → prompt/history/view → video artifact.

    The video contour is closed end to end: a real video Job is queued in CORE,
    a GPU Worker claims it over HTTP, the worker renders through the real
    ComfyUI socket and CORE imports the render as a ``video_scene`` artifact
    after re-verifying it structurally.
    """
    from worker import role_executor
    from core.app import store

    _video_runtime(comfyui)
    client, job, task, node_name, _auth = _dispatched_video_task(monkeypatch)
    artifacts = role_executor.execute_role_task("gpu", task)
    assert artifacts and artifacts[0]["kind"] == "video"
    contract = artifacts[0]["contract"]
    assert contract["format"] == "media_contract/v1"
    assert contract["mime_type"] == "video/mp4"
    assert contract["sha256"] == hashlib.sha256(MP4_VALID).hexdigest()
    assert contract["size"] == len(MP4_VALID)

    response = _post_result(client, job, task, node_name=node_name, artifacts=artifacts)
    assert response.status_code == 200, response.text
    # The render really travelled over the socket.
    paths = [path for _method, path in comfyui.requests]
    assert any(path == "/prompt" for path in paths)
    assert any(path.startswith("/history/") for path in paths)
    assert any(path.startswith("/view") for path in paths)
    # CORE stored the verified render as a video scene of the Job.
    stored = store.root / job.job_id / "video" / "scene-001.mp4"
    assert stored.is_file()
    assert stored.read_bytes() == MP4_VALID
    assert [item.kind for item in job.artifacts] == ["video_scene"]
    assert job.scenes[0].artifact_ids
    assert job.scenes[0].assigned_worker == node_name


# ── Issue #178.5: the fault matrix at the CORE contour ─────────────────────


def test_core_records_a_malformed_runtime_history_as_a_failed_attempt(comfyui, monkeypatch):
    """A broken runtime history must reach CORE as a failure, never as a render."""
    from worker import role_executor

    _video_runtime(comfyui)
    comfyui.history_override = {"prompt-1": {"outputs": {"9": {"unexpected": []}}}}
    client, job, task, node_name, _auth = _dispatched_video_task(monkeypatch)
    with pytest.raises(RuntimeError):
        role_executor.execute_role_task("gpu", task)
    response = _post_result(client, job, task, node_name=node_name, success=False,
                            error="ComfyUI completed without a video output")
    assert response.status_code == 200, response.text
    assert any("FAILED, RETRY" in event for event in job.events)
    assert not [item for item in job.artifacts if item.kind == "video_scene"]
    # The attempt is retried instead of silently lost.
    assert job.scenes[0].task_id and job.scenes[0].task_id != task["task_id"]


def test_core_records_a_render_timeout_as_a_failed_attempt(comfyui, monkeypatch):
    """A runtime that never answers must fail the attempt, not hang the Job."""
    from adapters.comfyui import ComfyUIAdapter
    from worker import role_executor

    _video_runtime(comfyui)
    comfyui.never_finishes = True
    monkeypatch.setattr("adapters.comfyui.time.sleep", lambda _seconds: None)
    # The real waiter is used, only its deadline is shortened so the test does
    # not spend the production timeout in a busy loop.
    original_wait = ComfyUIAdapter.wait_for_result
    monkeypatch.setattr("adapters.comfyui.ComfyUIAdapter.wait_for_result",
                        lambda self, prompt_id, timeout=300: original_wait(self, prompt_id, timeout=1))
    client, job, task, node_name, _auth = _dispatched_video_task(monkeypatch)
    with pytest.raises(TimeoutError):
        role_executor.execute_role_task("gpu", task)
    response = _post_result(client, job, task, node_name=node_name, success=False,
                            error="ComfyUI prompt timed out")
    assert response.status_code == 200, response.text
    assert any("FAILED, RETRY" in event for event in job.events)
    assert not [item for item in job.artifacts if item.kind == "video_scene"]


def test_core_refuses_a_late_result_of_an_imported_attempt(comfyui, monkeypatch):
    """A result that arrives twice must never overwrite the accepted render."""
    from worker import role_executor
    from core.app import store

    _video_runtime(comfyui)
    client, job, task, node_name, _auth = _dispatched_video_task(monkeypatch)
    artifacts = role_executor.execute_role_task("gpu", task)
    assert _post_result(client, job, task, node_name=node_name,
                        artifacts=artifacts).status_code == 200
    stored = (store.root / job.job_id / "video" / "scene-001.mp4").read_bytes()
    late = _post_result(client, job, task, node_name=node_name, artifacts=artifacts)
    assert late.status_code == 409, late.text
    assert "Duplicate result" in late.text
    assert (store.root / job.job_id / "video" / "scene-001.mp4").read_bytes() == stored


def test_core_refuses_a_result_from_a_node_that_does_not_own_the_attempt(comfyui, monkeypatch):
    """A foreign node must not be able to finish an attempt of another node."""
    client, job, task, _node_name, _auth = _dispatched_video_task(monkeypatch)
    response = _post_result(client, job, task, node_name=_unique_node("gpu-foreign"),
                            success=True, artifacts=[])
    assert response.status_code == 409, response.text
    assert "assigned to another worker" in response.text


def test_core_does_not_dispatch_a_video_task_to_a_node_without_enough_vram(comfyui, monkeypatch):
    """A node that cannot hold the render must not be handed the task."""
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    monkeypatch.setattr("core.security._valid_worker_request", lambda node, request: True)
    monkeypatch.setattr("core.api.tasks._finalize_and_notify", lambda job, images: None)
    client = _core_client()
    small = _unique_node("gpu-small")
    auth = _register_gpu(client, small, vram_mb=8192)
    job = _video_job(monkeypatch, min_vram_mb=32768)
    refused = client.post("/api/tasks/claim",
                          json={"node_name": small, "vram_mb": 8192},
                          headers=auth).json()
    assert refused.get("task") is None
    # A node that can hold the render gets the task.
    big = _unique_node("gpu-big")
    _register_gpu(client, big, vram_mb=32768, auth=auth)
    for _ in range(200):
        claimed = client.post("/api/tasks/claim",
                              json={"node_name": big, "vram_mb": 32768},
                              headers=auth).json().get("task")
        if claimed and claimed.get("job_id") == job.job_id:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("the video task was not dispatched to a capable node")
    assert claimed["min_vram_mb"] == 32768


def test_cancelling_one_job_never_reaches_another_node(comfyui, monkeypatch):
    """The cancellation of one Job must not interrupt the work of another node."""
    client, job_a, task_a, node_a, auth = _dispatched_video_task(monkeypatch)
    _client_b, job_b, task_b, node_b, _auth_b = _dispatched_video_task(monkeypatch)
    assert node_a != node_b

    client.post(f"/api/jobs/{job_a.job_id}/cancel")
    cancellations_a = client.get(f"/api/tasks/cancellations/{node_a}", headers=auth).json()
    cancellations_b = client.get(f"/api/tasks/cancellations/{node_b}", headers=auth).json()
    assert [item["task_id"] for item in cancellations_a] == [task_a["task_id"]]
    assert cancellations_b == []
    # The untouched Job keeps its own attempt alive.
    assert job_b.active_task_ids.get(task_b["task_id"]) == job_b.scenes[0].scene_id
