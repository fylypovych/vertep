"""Issue #468 (i.0.0.4.68): LAB /invoke does not block the event loop during inference.

The real ``lab.role_service`` app runs under uvicorn; only the model endpoint
is a controlled local HTTP server that holds its answer.  Neither ``/invoke``
nor ``_call_model`` is replaced.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import uvicorn

from lab import role_service

TOKEN = "executor-token-468"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def model_endpoint():
    state = {"calls": [], "received": threading.Event(), "release": threading.Event(),
             "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append({"path": self.path, "body": body,
                                   "lease": self.headers.get("X-Vertep-Lab-Lease")})
            state["received"].set()
            state["release"].wait(timeout=30)
            data = json.dumps({"response": "звіт", "eval_count": 7}).encode()
            try:
                self.send_response(state["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                pass  # client already gave up (timeout path)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        state["release"].set()
        server.shutdown()
        server.server_close()


@pytest.fixture
def api(monkeypatch, model_endpoint, tmp_path):
    monkeypatch.setenv("LAB_ROLE_EXECUTOR_TOKEN", TOKEN)
    monkeypatch.setenv("LAB_RESOURCE_URL", model_endpoint["url"])
    monkeypatch.setenv("LAB_RESOURCE_TOKEN", "resource-token")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(role_service.create_app(), host="127.0.0.1",
                                           port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "role service did not start"
        time.sleep(0.05)
    (tmp_path / "a.py").write_text("code")
    try:
        yield {"url": f"http://127.0.0.1:{port}", "root": str(tmp_path)}
    finally:
        model_endpoint["release"].set()
        server.should_exit = True
        thread.join(timeout=15)


def _invoke(api, *, limits=None, lease="lease-468", token=TOKEN):
    payload = {"task": {"kind": "audit", "payload": {"paths": ["a.py"]}}, "model": "ollama:m",
               "role": "reviewer", "workspace": {"root": api["root"]}, "limits": limits or {}}
    if lease:
        payload["resource_lease"] = {"lease_id": lease}
    return httpx.post(api["url"] + "/invoke", json=payload, timeout=30,
                      headers={"x-lab-executor-token": token})


def _in_background(call):
    box = {}
    thread = threading.Thread(target=lambda: box.update(response=call()), daemon=True)
    thread.start()
    return thread, box


def test_other_requests_are_served_while_inference_is_pending(api, model_endpoint):
    thread, box = _in_background(lambda: _invoke(api))
    assert model_endpoint["received"].wait(timeout=10), "model endpoint was not called"
    started = time.monotonic()
    health = httpx.get(api["url"] + "/health", timeout=5)
    assert health.status_code == 200 and health.json() == {"status": "HEALTHY"}
    rejected = httpx.post(api["url"] + "/invoke", json={}, timeout=5)
    assert rejected.status_code == 401, "auth still enforced on a concurrent request"
    assert time.monotonic() - started < 5
    assert thread.is_alive() and "response" not in box, "inference must still be pending"

    model_endpoint["release"].set()
    thread.join(timeout=15)
    answer = box["response"].json()
    assert answer["status"] == "ok" and answer["tokens_used"] == 7
    assert answer["result"]["summary"] == "звіт"
    assert len(model_endpoint["calls"]) == 1, "no duplicated model call"
    assert model_endpoint["calls"][0]["lease"] == "lease-468"
    assert model_endpoint["calls"][0]["path"] == "/inference/api/generate"


def test_model_timeout_is_failed_not_success(api, model_endpoint):
    answer = _invoke(api, limits={"timeout_seconds": 0.5}).json()
    assert answer["status"] == "failed"
    assert "result" not in answer
    assert len(model_endpoint["calls"]) == 1


def test_model_error_is_failed(api, model_endpoint):
    model_endpoint["status"] = 500
    model_endpoint["release"].set()
    answer = _invoke(api).json()
    assert answer["status"] == "failed"
    assert len(model_endpoint["calls"]) == 1


def test_lease_and_token_still_required_without_model_call(api, model_endpoint):
    assert _invoke(api, token="wrong").status_code == 401
    assert _invoke(api, lease=None).json() == {"status": "blocked", "reason": "no active GPU lease"}
    assert model_endpoint["calls"] == []
