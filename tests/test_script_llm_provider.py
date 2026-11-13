"""Issue #466 (i.0.0.4.66): the Worker script task uses the selected LLMProvider.

Controlled integration: effective provider configuration delivered to the
Worker -> ``execute_role_task("text", script)`` -> ``providers.llm()`` -> the
selected backend over real HTTP (local stub servers) -> script artifact.
No paid/live API is used and CORE performs no generation.
"""

import base64
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adapters.llm_clients import LLMBackendError
from tests.script_llm_stub import staged_response
from worker import role_executor

SECRET = "controlled-secret-value-466"
SCRIPT = {"title": "Провайдерний сценарій", "description": "опис", "hashtags": ["#vertep"],
          "voiceover": "озвучка",
          "scenes": [{"prompt": "image one", "video_prompt": "video one", "voiceover": "раз", "duration": 4},
                     {"prompt": "image two", "video_prompt": "video two", "voiceover": "два", "duration": 5}]}


@pytest.fixture
def backend():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "body": body,
                          "authorization": self.headers.get("Authorization")})
            if self.path == "/api/generate":
                answer = staged_response(body["prompt"], SCRIPT)
                payload = {"response": json.dumps(answer, ensure_ascii=False)}
            elif self.path == "/v1/chat/completions":
                answer = staged_response(body["messages"][-1]["content"], SCRIPT)
                payload = {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]}
            else:
                self.send_error(404)
                return
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}", "calls": calls}
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def worker_env(monkeypatch, backend):
    import adapters.providers as providers_module

    monkeypatch.setattr(providers_module, "_providers", None)
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_URL", backend["url"])
    monkeypatch.setenv("OLLAMA_MODEL", "controlled-ollama")
    monkeypatch.delenv("OLLAMA_SCRIPT_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", backend["url"] + "/v1")
    monkeypatch.setenv("OPENAI_MODEL", "controlled-openai")
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    return backend


def deliver(selection):
    """Apply the non-secret selection exactly as a CORE heartbeat delivers it."""
    from worker.service import apply_provider_config
    return apply_provider_config({"VERTEP_LLM_PROVIDER": selection})


def task():
    return {"task": "script", "topic": "провайдери", "system_prompt": "Ти — оповідач.",
            "character": {"visual": {"style": "графіка"}}, "revision": "додай гумору"}


def run_script():
    [artifact] = role_executor.execute_role_task("text", task())
    assert artifact["kind"] == "script" and artifact["filename"] == "script.json"
    return artifact, json.loads(base64.b64decode(artifact["data_base64"]).decode("utf-8"))


def assert_script(script):
    from core.script_schema import ScriptDocument
    ScriptDocument.model_validate(script)
    assert script["title"] == "Провайдерний сценарій"
    assert [scene["prompt"] for scene in script["scenes"]] == ["image one", "image two"]


def test_ollama_selection_reaches_ollama_backend(worker_env):
    assert deliver("ollama") == {"VERTEP_LLM_PROVIDER": "ollama"}
    _, script = run_script()
    assert_script(script)
    calls = worker_env["calls"]
    assert [call["path"] for call in calls] == ["/api/generate"] * 3
    assert all(call["body"]["model"] == "controlled-ollama" for call in calls)
    assert all("додай гумору" in call["body"]["prompt"] for call in calls)
    assert all("графіка" in call["body"]["prompt"] for call in calls)


def test_openai_selection_reaches_openai_compatible_backend(worker_env, caplog):
    caplog.set_level(logging.DEBUG)
    assert deliver("openai") == {"VERTEP_LLM_PROVIDER": "openai"}
    artifact, script = run_script()
    assert_script(script)
    calls = worker_env["calls"]
    assert [call["path"] for call in calls] == ["/v1/chat/completions"] * 3, "no silent Ollama"
    assert all(call["body"]["model"] == "controlled-openai" for call in calls)
    assert all(call["authorization"] == f"Bearer {SECRET}" for call in calls)
    assert all("додай гумору" in call["body"]["messages"][-1]["content"] for call in calls)
    assert SECRET not in json.dumps(artifact)
    assert SECRET not in caplog.text


def test_provider_switch_applies_to_next_task_without_restart(worker_env):
    deliver("ollama")
    run_script()
    deliver("openai")
    run_script()
    paths = [call["path"] for call in worker_env["calls"]]
    assert paths == ["/api/generate"] * 3 + ["/v1/chat/completions"] * 3


def test_unsupported_backend_fails_explicitly_without_fallback(worker_env, monkeypatch):
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "mystery-llm")
    with pytest.raises(LLMBackendError, match="Unsupported LLM backend 'mystery-llm'"):
        role_executor.execute_role_task("text", task())
    assert worker_env["calls"] == [], "must not fall back to Ollama or demo"


def test_openai_without_key_fails_explicitly(worker_env, monkeypatch):
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMBackendError, match="no API key"):
        role_executor.execute_role_task("text", task())
    assert worker_env["calls"] == []


def test_unavailable_backend_fails_explicitly(worker_env, monkeypatch, caplog):
    import httpx
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    with pytest.raises(httpx.HTTPError) as error:
        role_executor.execute_role_task("text", {**task(), "timeout": 2})
    assert SECRET not in str(error.value)
    assert SECRET not in caplog.text
    assert worker_env["calls"] == []


def test_script_executor_has_no_backend_protocol():
    import inspect
    source = inspect.getsource(role_executor.execute_script)
    assert "/api/generate" not in source and "OLLAMA_URL" not in source
    assert "providers.llm()" in source


def test_unsupported_backend_delivery_is_a_failed_activation(worker_env):
    import os
    observed = deliver("mystery-llm")
    assert os.environ["VERTEP_LLM_PROVIDER"] == "ollama", "previous selection restored"
    assert observed.get("VERTEP_LLM_PROVIDER") == "ollama"
