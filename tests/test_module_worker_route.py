"""Production Worker route with real controlled HTTP outputs, switch and rollback."""

import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from worker.service import execute_task
from worker.migration import digest, overview


@pytest.fixture
def runtimes():
    calls = []
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(image, format="PNG")
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b"\x01\x10\xff\xef" * 8000)
    class Handler(BaseHTTPRequestHandler):
        def send(self, data, mime="application/json"):
            if isinstance(data, dict):
                data = json.dumps(data).encode()
            self.send_response(200); self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            if self.path == "/api/generate":
                self.send({"response": "Actual controlled text"})
            elif self.path == "/prompt":
                self.send({"prompt_id": "p"})
            elif self.path == "/synthesize":
                self.send(audio.getvalue(), "audio/wav")
            else:
                self.send_error(404)
        def do_GET(self):
            if self.path.startswith("/history/"):
                self.send({"p": {"outputs": {"1": {"images": [{"filename": "actual.png"}]}}}})
            elif self.path.startswith("/view"):
                self.send(image.getvalue(), "image/png")
            else:
                self.send_error(404)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


@pytest.mark.parametrize("role,name,task_type", [("text", "text", "text"), ("gpu", "image", "image"), ("voice", "audio", "voice")])
def test_stock_worker_shadow_switch_rollback(role, name, task_type, runtimes, tmp_path, monkeypatch):
    url, calls = runtimes
    monkeypatch.setenv("NODE_ROLE", role)
    monkeypatch.setenv("NODE_NAME", "controlled-worker")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("OLLAMA_URL", url)
    monkeypatch.setenv("TTS_URL", url)
    monkeypatch.setenv("MODULE_STATE_ROOT", str(tmp_path / "module-state"))
    from adapters.providers.compute_backends import ComfyUIDistributedProvider
    import worker.role_executor as owner
    backend = ComfyUIDistributedProvider(url=url)
    monkeypatch.setattr(owner.providers, "compute", lambda: backend)
    workflows = tmp_path / "workflows"
    (workflows / "image").mkdir(parents=True)
    (workflows / "image/demo.json").write_text('{"1":{"inputs":{"text":"{{TOPIC}}"}}}')
    monkeypatch.setenv("WORKFLOWS_ROOT", str(workflows))
    outputs = []
    for index, mode in enumerate(("role", "shadow", "module", "role")):
        monkeypatch.setenv(f"VERTEP_{name.upper()}_MODULE_ENABLED", str(mode == "module").lower())
        monkeypatch.setenv(f"VERTEP_{name.upper()}_MODULE_SHADOW", str(mode == "shadow").lower())
        result = execute_task(None, {"task": task_type, "task_id": f"migration-{role}-{index}", "job_id": "migration-job",
            "lease_id": f"lease-{index}", "topic": "Actual migration", "voice": "uk", "provider": "controlled",
            "workflow": "workflows/image/demo.json"}, "controlled-worker")
        assert result["success"], result
        outputs.append(digest(result["artifacts"]))
        last = overview()[name]["last_execution"]
        assert last["path"] == mode and last["owner"] == "controlled-worker"
        if mode == "shadow":
            assert last["parity"] is True and last["external_effects"] == 1
            assert not (tmp_path / "module-state" / name / "state.json").exists()
    assert len(set(outputs)) == 1
    assert len(calls) == 4  # Shadow computes business logic twice, provider only once.
    state = json.loads((tmp_path / "module-state" / name / "state.json").read_text())
    assert state["last_task"] == f"migration-{role}-2"
