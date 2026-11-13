"""#461: the installed-model list comes from a real Ollama HTTP endpoint, and its failure
is reported instead of being turned into an empty list.

A controlled local HTTP server stands in for Ollama; the consumer is the actual route.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import system
from core.deployment_plan import create_plan
from core.node_registry import node_roles


class FakeOllama:
    def __init__(self):
        self.models = [{"name": "llama3.2:3b", "size": 1}]
        self.status = 200
        self.body = None
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                owner.requests.append((self.command, self.path, self.rfile.read(length).decode() if length else ""))
                payload = owner.body if owner.body is not None else json.dumps({"models": owner.models})
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload.encode())

            do_GET = do_DELETE = _answer

            def log_message(self, *_args):
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _closed_port_url():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("NODE_ROLE", "core")
    monkeypatch.delenv("NODE_ADDITIONAL_ROLES", raising=False)
    (tmp_path / "installation.json").write_text(json.dumps({"completed_at": "now", "node_role": "core",
                                                             "ai_backend": {"type": "skip"}}))
    app = FastAPI()
    app.include_router(system.router)
    ollama = FakeOllama()
    yield TestClient(app), tmp_path, ollama
    ollama.close()


def _select_text(root, selected=True):
    plan = create_plan(node_roles(), "core", system.application_version(), ["text"] if selected else [])
    (root / "deployment-plan.json").write_text(json.dumps(plan))


def test_installed_models_come_from_the_local_ollama(api, monkeypatch):
    client, root, ollama = api
    _select_text(root)
    monkeypatch.setenv("OLLAMA_URL", ollama.url)
    body = client.get("/api/system/models").json()
    assert body["local_status"] == "ok" and [item["name"] for item in body["models"]] == ["llama3.2:3b"]
    assert ("GET", "/api/tags", "") in ollama.requests
    assert "nodes" in body
    ollama.models = []
    empty = client.get("/api/system/models").json()
    assert empty["local_status"] == "ok" and empty["models"] == []


@pytest.mark.parametrize("fault", ["unreachable", "http_500", "malformed"])
def test_list_failure_is_reported_and_recovery_needs_no_reload(api, monkeypatch, fault):
    client, root, ollama = api
    _select_text(root)
    if fault == "unreachable":
        monkeypatch.setenv("OLLAMA_URL", _closed_port_url())
    else:
        monkeypatch.setenv("OLLAMA_URL", ollama.url)
        if fault == "http_500":
            ollama.status = 500
        else:
            ollama.body = '{"models": "not-a-list"}'
    failed = client.get("/api/system/models")
    assert failed.status_code == 200  # per-node placement stays available
    body = failed.json()
    assert body["local_status"] == "unavailable" and body["models"] == [] and body["local_error"]
    assert "127.0.0.1" not in body["local_error"]  # sanitized: no internal address
    # The dependency recovers: the same request returns the actual list.
    ollama.status, ollama.body = 200, None
    monkeypatch.setenv("OLLAMA_URL", ollama.url)
    recovered = client.get("/api/system/models").json()
    assert recovered["local_status"] == "ok" and recovered["models"][0]["name"] == "llama3.2:3b"


def test_core_without_local_ollama_is_not_an_error(api, monkeypatch):
    client, root, _ollama = api
    _select_text(root, selected=False)
    monkeypatch.setenv("OLLAMA_URL", _closed_port_url())
    assert client.get("/api/system/models").json()["local_status"] == "not_selected"
    monkeypatch.delenv("OLLAMA_URL")
    assert client.get("/api/system/models").json()["local_status"] == "not_configured"


def test_local_delete_reaches_ollama(api, monkeypatch):
    client, root, ollama = api
    monkeypatch.setenv("OLLAMA_URL", ollama.url)
    response = client.delete("/api/system/models/llama3.2:3b")
    assert response.status_code == 200
    assert ollama.requests[-1][:2] == ("DELETE", "/api/delete")
    assert json.loads(ollama.requests[-1][2]) == {"name": "llama3.2:3b"}
