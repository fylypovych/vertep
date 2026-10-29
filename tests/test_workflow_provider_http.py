"""All agreed provider types against controlled endpoints; no live accounts."""

import base64
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from core.gpu.backends import configured_router


@pytest.fixture
def endpoints():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def send(self, value, content_type="application/json"):
            data = json.dumps(value).encode() if content_type == "application/json" else value
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            calls.append(("GET", path))
            if path == "/api/tags":
                self.send({"models": [{"name": "controlled-model"}]})
            elif path == "/models":
                self.send({"data": [{"id": "controlled-model"}]})
            elif path == "/system_stats":
                self.send({"devices": [{"vram_total": 4096 * 1048576, "vram_free": 4096 * 1048576}]})
            elif path == "/status":
                self.send({"gpu_available": True, "contract_version": "1"})
            elif path.startswith("/history/"):
                self.send({"controlled-prompt": {"outputs": {"1": {"images": [{"filename": "actual.png"}]}}}})
            elif path == "/view":
                self.send(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1sAAAAASUVORK5CYII="), "image/png")
            else:
                self.send_error(404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path))
            if self.path == "/api/generate":
                assert body["model"] == "controlled-model"
                self.send({"response": "actual controlled inference"})
            elif self.path == "/chat/completions":
                assert self.headers["Authorization"] == "Bearer controlled-test-key"
                self.send({"choices": [{"message": {"content": "actual controlled inference"}}]})
            elif self.path == "/prompt":
                self.send({"prompt_id": "controlled-prompt"})
            elif self.path == "/lease/request":
                self.send({"granted": True, "contract_version": "1", "lease": {
                    "lease_id": "controlled-lease", "endpoint": endpoint,
                    "model": "controlled-model", "granted_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}})
            elif self.path == "/lease/release":
                self.send({"released": True, "contract_version": "1"})
            elif self.path == "/lease/heartbeat":
                self.send({"active": True, "state": "GRANTED", "contract_version": "1",
                           "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()})
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    endpoint = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield endpoint, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("kind", ["local_model", "ollama", "openai", "comfyui", "lab"])
def test_configured_provider_execution(kind, endpoints, tmp_path, monkeypatch):
    endpoint, calls = endpoints
    monkeypatch.setenv("CONTROLLED_OPENAI_KEY", "controlled-test-key")
    workflows = tmp_path / "workflows"
    (workflows / "image").mkdir(parents=True)
    (workflows / "image/test.json").write_text('{"1":{"inputs":{"text":"{{TOPIC}}"}}}')
    monkeypatch.setenv("WORKFLOWS_ROOT", str(workflows))
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"local_only": False, "providers": [{"name": "configured", "type": kind,
        "endpoint": endpoint, "model": "controlled-model", "key_env": "CONTROLLED_OPENAI_KEY", "priority": 1,
        "resources": {"vram_total_mb": 4096, "vram_used_mb": 0}}]}))
    local_calls = []
    def local(task):
        local_calls.append(task["task_id"])
        return [{"filename": "actual.txt", "kind": "text", "data_base64": base64.b64encode(b"local executor output").decode()}]
    router = configured_router(config, local)
    capability = "image_generation" if kind == "comfyui" else "text_generation"
    result = router.execute(capability, {"task_id": "controlled-task", "topic": "topic", "model": "controlled-model", "workflow": "workflows/image/test.json"})
    assert result["status"] == "complete"
    assert base64.b64decode(result["artifacts"][0]["data_base64"])
    if kind == "local_model":
        assert local_calls == ["controlled-task"]
        assert not calls
    else:
        expected = {"ollama": "/api/generate", "openai": "/chat/completions", "comfyui": "/prompt", "lab": "/lease/request"}[kind]
        assert ("POST", expected) in calls
    if kind == "lab":
        assert ("POST", "/lease/release") in calls


def test_local_only_prevents_cloud_readiness_and_submission(endpoints, tmp_path, monkeypatch):
    endpoint, calls = endpoints
    monkeypatch.setenv("CONTROLLED_OPENAI_KEY", "controlled-test-key")
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"local_only": True, "providers": [{"name": "cloud", "type": "openai", "endpoint": endpoint,
        "model": "controlled-model", "key_env": "CONTROLLED_OPENAI_KEY", "priority": 1}]}))
    router = configured_router(config, lambda task: [])
    with pytest.raises(RuntimeError, match="exhausted"):
        router.execute("text_generation", {"task_id": "x"})
    assert calls == []
