"""Worker-side model lifecycle — Issue i.0.0.0.78 remainder.

Covers the node-local half of the per-node model pull contract: catalog
reporting (role gating + cache) and heartbeat model command execution
(dedup, cancel, progress streaming, unsupported actions).
"""

import json
import threading
import time

import httpx
import pytest

import worker.service as worker_service
from worker.service import (
    _MODEL_CATALOG_CACHE,
    _MODEL_COMMANDS,
    _MODEL_COMMANDS_LOCK,
    _MODEL_COMMAND_LIMIT,
    _run_model_command,
    handle_model_command,
    text_model_catalog,
)


@pytest.fixture(autouse=True)
def _clean_state():
    def _reset():
        with _MODEL_COMMANDS_LOCK:
            _MODEL_COMMANDS.clear()
        _MODEL_CATALOG_CACHE.update(value={}, loaded=False, checked_at=0.0)
    _reset()
    yield
    _reset()


def _op_id(seed: str = "a") -> str:
    return (seed * 32)[:32]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeStream:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self):
        return iter(self._lines)


class _FakeClient:
    lines: list[str] = []

    def __init__(self, *_args, **_kwargs):
        pass

    def stream(self, *_args, **_kwargs):
        return _FakeStream(list(self.lines))

    def request(self, *_args, **_kwargs):
        raise AssertionError("not used in these tests")

    def close(self):
        return None


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

class TestTextModelCatalog:
    def test_non_text_role_reports_no_catalog(self, monkeypatch):
        monkeypatch.setenv("NODE_ROLE", "gpu")
        monkeypatch.setattr(
            worker_service.httpx, "get",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
        assert text_model_catalog() == {}

    def test_text_role_fetches_sorted_catalog(self, monkeypatch):
        monkeypatch.setenv("NODE_ROLE", "text")
        monkeypatch.delenv("NODE_CAPABILITIES", raising=False)
        calls = {"n": 0}

        def _get(url, **_kwargs):
            calls["n"] += 1
            assert url.endswith("/api/tags")
            return _FakeResponse({"models": [{"name": "zeta"}, {"name": "alpha"},
                                              {"name": "alpha"}]})

        monkeypatch.setattr(worker_service.httpx, "get", _get)
        assert text_model_catalog() == {"models": ["alpha", "zeta"]}
        assert calls["n"] == 1

    def test_catalog_is_cached_between_heartbeats(self, monkeypatch):
        monkeypatch.setenv("NODE_ROLE", "text")
        monkeypatch.setenv("MODEL_CATALOG_INTERVAL_SECONDS", "30")
        calls = {"n": 0}

        def _get(*_a, **_k):
            calls["n"] += 1
            return _FakeResponse({"models": [{"name": "m1"}]})

        monkeypatch.setattr(worker_service.httpx, "get", _get)
        assert text_model_catalog() == {"models": ["m1"]}
        assert text_model_catalog() == {"models": ["m1"]}
        assert calls["n"] == 1

    def test_cache_expires_after_interval(self, monkeypatch):
        monkeypatch.setenv("NODE_ROLE", "text")
        monkeypatch.setenv("MODEL_CATALOG_INTERVAL_SECONDS", "0")
        calls = {"n": 0}

        def _get(*_a, **_k):
            calls["n"] += 1
            return _FakeResponse({"models": [{"name": f"m{calls['n']}"}]})

        monkeypatch.setattr(worker_service.httpx, "get", _get)
        assert text_model_catalog() == {"models": ["m1"]}
        assert text_model_catalog() == {"models": ["m2"]}

    def test_capability_overrides_role(self, monkeypatch):
        monkeypatch.setenv("NODE_ROLE", "gpu")
        monkeypatch.setenv("NODE_CAPABILITIES", "text_generation,image_generation")
        monkeypatch.setattr(
            worker_service.httpx, "get",
            lambda *a, **k: _FakeResponse({"models": [{"name": "m"}]}))
        assert text_model_catalog() == {"models": ["m"]}

    def test_runtime_error_keeps_previous_catalog(self, monkeypatch):
        monkeypatch.setenv("NODE_ROLE", "text")
        state = {"fail": False}

        def _get(*_a, **_k):
            if state["fail"]:
                raise httpx.ConnectError("down")
            return _FakeResponse({"models": [{"name": "m1"}]})

        monkeypatch.setattr(worker_service.httpx, "get", _get)
        assert text_model_catalog() == {"models": ["m1"]}
        state["fail"] = True
        monkeypatch.setenv("MODEL_CATALOG_INTERVAL_SECONDS", "0")
        assert text_model_catalog() == {"models": ["m1"]}

    def test_first_failure_reports_empty_catalog(self, monkeypatch):
        monkeypatch.setenv("NODE_ROLE", "text")
        monkeypatch.setattr(
            worker_service.httpx, "get",
            lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
        assert text_model_catalog() == {}


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

class TestHandleModelCommand:
    def test_ignores_non_dict_and_bad_operation_id(self, monkeypatch):
        spawned = []
        monkeypatch.setattr(
            worker_service, "_run_model_command",
            lambda *a, **k: spawned.append(a))
        handle_model_command(None, "http://core", "node", {})
        handle_model_command("nope", "http://core", "node", {})
        handle_model_command({"operation_id": "short", "action": "pull"},
                             "http://core", "node", {})
        handle_model_command({"operation_id": "zz" * 16, "action": "pull"},
                             "http://core", "node", {})
        assert spawned == []

    def test_starts_command_once(self, monkeypatch):
        spawned = []
        gate = threading.Event()

        def _stub(*_a, **_k):
            spawned.append(1)
            gate.wait(2)

        monkeypatch.setattr(worker_service, "_run_model_command", _stub)
        command = {"operation_id": _op_id(), "action": "pull", "model": "m"}
        handle_model_command(command, "http://core", "node", {})
        handle_model_command(command, "http://core", "node", {})
        gate.set()
        time.sleep(0.05)
        assert len(spawned) == 1

    def test_cancel_sets_existing_event(self, monkeypatch):
        spawned = []
        gate = threading.Event()

        def _stub(*_a, **_kwargs):
            spawned.append(1)
            gate.wait(2)

        monkeypatch.setattr(worker_service, "_run_model_command", _stub)
        command = {"operation_id": _op_id("b"), "action": "pull", "model": "m"}
        handle_model_command(command, "http://core", "node", {})
        with _MODEL_COMMANDS_LOCK:
            entry = _MODEL_COMMANDS[command["operation_id"]]
            assert entry["thread"].is_alive()
        handle_model_command(
            {"operation_id": command["operation_id"], "action": "cancel"},
            "http://core", "node", {})
        assert entry["cancel"].is_set()
        gate.set()
        time.sleep(0.05)
        assert len(spawned) == 1

    def test_prunes_dead_entries_over_limit(self, monkeypatch):
        spawned = []
        monkeypatch.setattr(
            worker_service, "_run_model_command",
            lambda *a, **k: spawned.append(1))
        for i in range(_MODEL_COMMAND_LIMIT + 6):
            thread = threading.Thread(target=lambda: None)
            thread.start()
            thread.join()
            with _MODEL_COMMANDS_LOCK:
                _MODEL_COMMANDS[f"{i:032x}"] = {
                    "thread": thread, "cancel": threading.Event()}
        with _MODEL_COMMANDS_LOCK:
            assert len(_MODEL_COMMANDS) > _MODEL_COMMAND_LIMIT
        handle_model_command({"operation_id": _op_id("c"), "action": "pull"},
                             "http://core", "node", {})
        with _MODEL_COMMANDS_LOCK:
            assert len(_MODEL_COMMANDS) == 1
        time.sleep(0.05)
        assert spawned == [1]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class TestRunModelCommand:
    def _capture(self, monkeypatch, cancel_results=None):
        calls = []
        results = cancel_results or []

        def _report(client, core, node_name, operation_id, status, phase,
                    progress, error=None):
            calls.append({"status": status, "phase": phase,
                          "progress": progress, "error": error})
            if results:
                return results.pop(0)
            return False

        monkeypatch.setattr(worker_service, "_report_model_progress", _report)
        return calls

    def test_cancel_action_reports_cancelled(self, monkeypatch):
        calls = self._capture(monkeypatch)
        _run_model_command("http://core", "node",
                           {"operation_id": _op_id(), "action": "cancel"},
                           {}, threading.Event())
        assert [c["status"] for c in calls] == ["CANCELLED"]
        assert calls[0]["error"]

    def test_unsupported_action_fails(self, monkeypatch):
        calls = self._capture(monkeypatch)
        _run_model_command("http://core", "node",
                           {"operation_id": _op_id(), "action": "explode"},
                           {}, threading.Event())
        assert calls[-1]["status"] == "FAILED"
        assert "Unsupported" in calls[-1]["error"]

    def test_pull_streams_progress_to_completion(self, monkeypatch):
        calls = self._capture(monkeypatch)
        monkeypatch.setattr(worker_service.httpx, "Client", _FakeClient)
        _FakeClient.lines = [
            json.dumps({"status": "pulling manifest"}),
            json.dumps({"completed": 50, "total": 100}),
            json.dumps({"completed": 100, "total": 100}),
            "not-json",
        ]
        _run_model_command("http://core", "node",
                           {"operation_id": _op_id(), "action": "pull",
                            "model": "llama3"},
                           {}, threading.Event())
        statuses = [c["status"] for c in calls]
        assert statuses[0] == "RUNNING"
        assert statuses[-1] == "COMPLETED"
        assert any(c["progress"] == 100 for c in calls)
        assert all(c["error"] is None for c in calls if c["status"] != "FAILED")

    def test_core_cancel_requested_stops_pull(self, monkeypatch):
        calls = self._capture(monkeypatch, cancel_results=[True])
        monkeypatch.setattr(worker_service.httpx, "Client", _FakeClient)
        _FakeClient.lines = [json.dumps({"completed": 10, "total": 100})]
        _run_model_command("http://core", "node",
                           {"operation_id": _op_id(), "action": "pull"},
                           {}, threading.Event())
        assert "CANCELLED" in [c["status"] for c in calls]
        assert "COMPLETED" not in [c["status"] for c in calls]

    def test_stream_error_event_fails(self, monkeypatch):
        calls = self._capture(monkeypatch)
        monkeypatch.setattr(worker_service.httpx, "Client", _FakeClient)
        _FakeClient.lines = [json.dumps({"error": "model not found"})]
        _run_model_command("http://core", "node",
                           {"operation_id": _op_id(), "action": "pull"},
                           {}, threading.Event())
        assert calls[-1]["status"] == "FAILED"
        assert "model not found" in calls[-1]["error"]

    def test_transport_failure_is_reported(self, monkeypatch):
        calls = self._capture(monkeypatch)

        class _Boom:
            def __init__(self, *_a, **_k):
                raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(worker_service.httpx, "Client", _Boom)
        _run_model_command("http://core", "node",
                           {"operation_id": _op_id(), "action": "pull"},
                           {}, threading.Event())
        assert calls[-1]["status"] == "FAILED"

    def test_entry_is_released_after_run(self, monkeypatch):
        self._capture(monkeypatch)
        monkeypatch.setattr(worker_service.httpx, "Client", _FakeClient)
        _FakeClient.lines = []
        operation_id = _op_id("d")
        cancel = threading.Event()
        with _MODEL_COMMANDS_LOCK:
            _MODEL_COMMANDS[operation_id] = {
                "thread": threading.Thread(target=lambda: None),
                "cancel": cancel}
        _run_model_command("http://core", "node",
                           {"operation_id": operation_id, "action": "pull"},
                           {}, cancel)
        with _MODEL_COMMANDS_LOCK:
            assert operation_id not in _MODEL_COMMANDS
