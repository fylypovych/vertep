"""Issue #201 (i.0.0.2.1): actual controlled HTTP proof for the model pull loops.

The earlier stalled-pull tests stubbed ``httpx.Client`` with stand-ins whose
``close()`` simply released a local Event, so they exercised the reader-loop
branches but never proved that cancellation aborts a real transport or that the
socket is released.  Both pull loops (CORE-local ``_pull_model`` and node-local
``_run_model_command``) are therefore driven here against a real controlled
HTTP server on 127.0.0.1:

* success      — the explicit terminal ``success`` event completes the pull;
* error        — an ``error`` NDJSON event fails it;
* EOF          — a clean end of stream without ``success`` fails it;
* HTTP status  — a non-200 answer fails it;
* stalled      — cancellation closes the live transport promptly, the server
                 observes the client releasing the socket, and a late result
                 never rewrites the terminal state;
* restart      — reconciliation of a persisted, interrupted pull keeps the
                 progress recorded before the restart.
"""

import json
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import worker.service as worker_service
from core.operations import advance_operation, begin_operation, create_operation, get_operation
from core.pull_executor import _CANCEL_EVENTS, cancel_pull, reconcile_model_pull_operations, start_pull
from worker.service import _run_model_command


# ---------------------------------------------------------------------------
# Controlled HTTP server
# ---------------------------------------------------------------------------


def _ndjson(*events):
    return [json.dumps(event) for event in events]


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _wait_terminal(operation_id, timeout=6.0):
    deadline = time.monotonic() + timeout
    stored = get_operation(operation_id)
    while time.monotonic() < deadline:
        stored = get_operation(operation_id)
        if stored and stored["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return stored
        time.sleep(0.02)
    return stored


class _PullScenario:
    """One controlled behaviour for the fake Ollama/CORE endpoints."""

    def __init__(self, kind, lines=()):
        self.kind = kind
        self.lines = [line if isinstance(line, bytes) else str(line).encode()
                      for line in lines]
        self.stream_started = threading.Event()
        self.disconnected = threading.Event()
        self.release = threading.Event()
        self.reports = []
        self.sent = 0
        self.cancel_from_report = None
        self.lock = threading.RLock()

    def answer_cancel(self) -> bool:
        with self.lock:
            if self.cancel_from_report is None:
                return False
            return len(self.reports) >= self.cancel_from_report


class _PullHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        return

    def _write_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self):
        scenario = self.server.scenario
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if self.path == "/api/workers/model-progress":
            with scenario.lock:
                scenario.reports.append(json.loads(raw or b"{}"))
                cancel_requested = scenario.answer_cancel()
            self._write_json({"cancel_requested": cancel_requested})
            return
        if self.path != "/api/pull":
            self._write_json({"error": "not found"}, status=404)
            return
        if scenario.kind == "http_500":
            self._write_json({"error": "upstream exploded"}, status=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        scenario.stream_started.set()
        for line in scenario.lines:
            try:
                self.wfile.write(line + b"\n")
                self.wfile.flush()
            except OSError:
                scenario.disconnected.set()
                return
            scenario.sent += 1
            time.sleep(0.05)
        if scenario.kind in {"stall", "stall_after_success"}:
            self._hold(scenario)

    def _hold(self, scenario):
        """Keep the response open (a stalled pull) until the client lets go."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if scenario.release.is_set():
                return
            ready, _, _ = select.select([self.connection], [], [], 0.05)
            if not ready:
                continue
            try:
                data = self.connection.recv(1)
            except OSError:
                data = b""
            if not data:
                scenario.disconnected.set()
                return


@pytest.fixture
def pull_http():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PullHandler)
    server.scenario = _PullScenario("idle")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class _Handle:
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def use(self, kind, lines=()):
            scenario = _PullScenario(kind, lines)
            server.scenario = scenario
            return scenario

    yield _Handle()

    # Release any held response so a pull thread that survived its assertion
    # cannot block the shared in-process executor during the next reset.
    server.scenario.release.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and _CANCEL_EVENTS:
        time.sleep(0.05)


@pytest.fixture(autouse=True)
def _clean_pull_state():
    yield
    _CANCEL_EVENTS.clear()
    with worker_service._MODEL_COMMANDS_LOCK:
        worker_service._MODEL_COMMANDS.clear()


# ---------------------------------------------------------------------------
# CORE-local pull over a real transport
# ---------------------------------------------------------------------------


def test_local_http_stream_success_completes_pull(pull_http, monkeypatch):
    scenario = pull_http.use("success", _ndjson(
        {"status": "pulling manifest"},
        {"completed": 50, "total": 100},
        {"completed": 100, "total": 100},
        {"status": "success"},
    ))
    monkeypatch.setenv("OLLAMA_URL", pull_http.base)
    operation = start_pull("http-success-model")
    assert operation["node_name"] is None
    stored = _wait_terminal(operation["operation_id"])
    assert scenario.stream_started.wait(timeout=3)
    assert stored["status"] == "COMPLETED"
    assert stored["progress"] == 100
    assert stored["result"]["pulled"] is True


def test_local_http_stream_error_event_fails_pull(pull_http, monkeypatch):
    pull_http.use("error", _ndjson({"error": "model not found"}))
    monkeypatch.setenv("OLLAMA_URL", pull_http.base)
    operation = start_pull("http-error-model")
    stored = _wait_terminal(operation["operation_id"])
    assert stored["status"] == "FAILED"
    assert "model not found" in stored["error"]


def test_local_http_eof_without_success_fails_and_keeps_progress(pull_http, monkeypatch):
    pull_http.use("eof", _ndjson({"completed": 100, "total": 100}))
    monkeypatch.setenv("OLLAMA_URL", pull_http.base)
    operation = start_pull("http-eof-model")
    stored = _wait_terminal(operation["operation_id"])
    assert stored["status"] == "FAILED"
    assert "terminal success" in stored["error"]
    assert stored["progress"] == 100


def test_local_http_status_error_fails_pull(pull_http, monkeypatch):
    pull_http.use("http_500")
    monkeypatch.setenv("OLLAMA_URL", pull_http.base)
    operation = start_pull("http-status-model")
    stored = _wait_terminal(operation["operation_id"])
    assert stored["status"] == "FAILED"
    assert "Ollama returned 500" in stored["error"]


def test_local_http_cancel_releases_stalled_transport(pull_http, monkeypatch):
    scenario = pull_http.use("stall", _ndjson({"status": "pulling manifest"}))
    monkeypatch.setenv("OLLAMA_URL", pull_http.base)
    operation = start_pull("http-stall-model")
    assert scenario.stream_started.wait(timeout=3)

    cancel_pull(operation["operation_id"], "user cancelled")
    stored = _wait_terminal(operation["operation_id"], timeout=6)

    # The reader was released by closing the transport, not by a read timeout.
    assert stored["status"] == "CANCELLED"
    assert operation["operation_id"] not in _CANCEL_EVENTS
    # Resource release: the server observes the client dropping the socket.
    assert scenario.disconnected.wait(timeout=5), "pull transport was not released"


def test_local_http_late_success_does_not_rewrite_cancelled_state(pull_http, monkeypatch):
    scenario = pull_http.use("stall_after_success", _ndjson(
        {"completed": 100, "total": 100},
        {"status": "success"},
    ))
    monkeypatch.setenv("OLLAMA_URL", pull_http.base)
    operation = start_pull("http-late-success-model")
    assert scenario.stream_started.wait(timeout=3)
    # The progress line was consumed while the explicit success event sits in
    # an still-open stream, so the loop is still waiting for the next line.
    assert _wait_until(
        lambda: get_operation(operation["operation_id"])["progress"] == 100, timeout=3)
    time.sleep(0.3)
    assert get_operation(operation["operation_id"])["status"] == "RUNNING"

    cancel_pull(operation["operation_id"], "user cancelled")
    stored = _wait_terminal(operation["operation_id"], timeout=6)
    assert stored["status"] == "CANCELLED"
    assert scenario.disconnected.wait(timeout=5)

    # A late success (and any late transport failure) must not rewrite it.
    from core.operations import complete_operation, fail_operation
    complete_operation(operation["operation_id"], {"pulled": True})
    fail_operation(operation["operation_id"], "late transport error")
    stored = get_operation(operation["operation_id"])
    assert stored["status"] == "CANCELLED"
    assert stored["progress"] == 100


def test_restart_reconcile_fails_local_pull_but_preserves_progress():
    operation = create_operation("model_pull", "test", target="http-restart-model")
    begin_operation(operation["operation_id"], "pull")
    advance_operation(operation["operation_id"], "pull", 60, "downloading layers")

    recovered = reconcile_model_pull_operations()

    stored = get_operation(operation["operation_id"])
    assert recovered >= 1
    assert stored["status"] == "FAILED"
    assert "CORE restart" in stored["error"]
    # The persisted progress survives the restart reconciliation.
    assert stored["progress"] == 60
    assert stored["message"] == "downloading layers"


# ---------------------------------------------------------------------------
# Node-local pull over a real transport
# ---------------------------------------------------------------------------


def _node_run(handle, operation_id, monkeypatch, cancel=None):
    monkeypatch.setenv("OLLAMA_URL", handle.base)
    cancel = cancel if cancel is not None else threading.Event()
    command = {"operation_id": operation_id, "action": "pull", "model": "llama3"}
    _run_model_command(handle.base, "http-node", command, {}, cancel)
    return cancel


def _statuses(scenario):
    return [report["status"] for report in scenario.reports]


def test_node_http_stream_success_reports_completed(pull_http, monkeypatch):
    scenario = pull_http.use("success", _ndjson(
        {"status": "pulling manifest"},
        {"completed": 100, "total": 100},
        {"status": "success"},
    ))
    _node_run(pull_http, "a" * 32, monkeypatch)
    assert scenario.stream_started.wait(timeout=3)
    assert _statuses(scenario)[-1] == "COMPLETED"
    assert any(report.get("progress") == 100 for report in scenario.reports)
    assert _statuses(scenario).count("FAILED") == 0


def test_node_http_stream_error_event_reports_failed(pull_http, monkeypatch):
    scenario = pull_http.use("error", _ndjson({"error": "model not found"}))
    _node_run(pull_http, "b" * 32, monkeypatch)
    assert _statuses(scenario)[-1] == "FAILED"
    assert "model not found" in scenario.reports[-1]["error"]
    assert "COMPLETED" not in _statuses(scenario)


def test_node_http_eof_without_success_reports_failed(pull_http, monkeypatch):
    scenario = pull_http.use("eof", _ndjson({"completed": 100, "total": 100}))
    _node_run(pull_http, "c" * 32, monkeypatch)
    assert _statuses(scenario)[-1] == "FAILED"
    assert "terminal success" in scenario.reports[-1]["error"]
    assert "COMPLETED" not in _statuses(scenario)


def test_node_http_status_error_reports_failed(pull_http, monkeypatch):
    scenario = pull_http.use("http_500")
    _node_run(pull_http, "d" * 32, monkeypatch)
    assert _statuses(scenario)[-1] == "FAILED"
    assert "Ollama returned 500" in scenario.reports[-1]["error"]
    assert "COMPLETED" not in _statuses(scenario)


def test_node_http_cancel_releases_stalled_transport(pull_http, monkeypatch):
    scenario = pull_http.use("stall", _ndjson({"status": "pulling manifest"}))
    monkeypatch.setenv("OLLAMA_URL", pull_http.base)
    cancel = threading.Event()
    thread = threading.Thread(
        target=_run_model_command,
        args=(pull_http.base, "http-node",
              {"operation_id": "e" * 32, "action": "pull", "model": "llama3"},
              {}, cancel),
    )
    thread.start()
    assert scenario.stream_started.wait(timeout=3)

    cancel.set()
    thread.join(timeout=6)

    # Aborted promptly by closing the transport, not by the read timeout.
    assert not thread.is_alive()
    assert _statuses(scenario)[-1] == "CANCELLED"
    assert "COMPLETED" not in _statuses(scenario)
    assert "FAILED" not in _statuses(scenario)
    assert scenario.disconnected.wait(timeout=5), "pull transport was not released"


def test_node_http_core_cancel_requested_stops_before_streaming(pull_http, monkeypatch):
    scenario = pull_http.use("stall", _ndjson({"status": "pulling manifest"}))
    scenario.cancel_from_report = 1
    _node_run(pull_http, "f" * 32, monkeypatch)
    assert _statuses(scenario)[-1] == "CANCELLED"
    assert "COMPLETED" not in _statuses(scenario)
    assert not scenario.stream_started.is_set()


def test_node_http_core_cancel_requested_stops_during_stream(pull_http, monkeypatch):
    scenario = pull_http.use("stall_after_success", _ndjson(
        {"completed": 10, "total": 100},
        {"status": "success"},
    ))
    # First report (RUNNING 0) still lets the pull start; the report produced
    # by the progress line asks CORE to stop, and the still-open response
    # guarantees the loop cannot reach a completion on its own.
    scenario.cancel_from_report = 2
    _node_run(pull_http, "0" * 32, monkeypatch)
    assert scenario.stream_started.wait(timeout=3)
    assert _statuses(scenario)[-1] == "CANCELLED"
    assert "COMPLETED" not in _statuses(scenario)
