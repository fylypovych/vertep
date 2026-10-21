"""Role executor invocation in the laboratory orchestrator (Issue i.0.0.2.50, L1).

These tests talk to a *real* local HTTP endpoint: the role runner contract is
exercised end to end (request body, headers, answers, refusal paths) instead of
being compared against a fixture dict.  They prove that

* a run reaches ``COMPLETED`` only through an actual invocation;
* a disabled, unreachable or erroring endpoint never yields a fake success;
* a read-only reviewer cannot execute change work and keeps its own result;
* paid providers and a non-isolated workspace stop the run before any call.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lab.budget import BudgetLedger
from lab.executor import (EndpointRoleExecutor, ExecutorError, ExecutorUnavailable,
                          executor_from_environment)
from lab.gpu_lease import FakeResourceProvider
from lab.orchestrator import Orchestrator, RunState
from lab.policy import PolicyGate
from lab.workspace import IsolationViolation, Workspace


# -- a controlled local role endpoint --------------------------------------
class _Endpoint:
    """Thread-safe recorder of every invocation the endpoint received."""

    def __init__(self, *, respond=None, status_code: int = 200, delay: float = 0.0):
        self.requests: list[dict] = []
        self.status_code = status_code
        self.delay = delay
        self.respond = respond or (lambda payload: {
            "status": "ok", "result": {"task_id": payload["task"]["task_id"]}})
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length))
                if state.delay:
                    time.sleep(state.delay)
                state.requests.append({"path": self.path,
                                       "headers": dict(self.headers),
                                       "payload": payload})
                body = json.dumps(state.respond(payload)).encode("utf-8")
                self.send_response(state.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def endpoint():
    service = _Endpoint()
    yield service
    service.close()


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "lab").mkdir()
    (root / "tests").mkdir()
    (root / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n")
    return Workspace(root, environ={})


@pytest.fixture()
def gate(tmp_path):
    return PolicyGate(tmp_path / "state", activated=True)


@pytest.fixture()
def ledger(tmp_path):
    return BudgetLedger(tmp_path / "state" / "budget")


def _executor(url: str, **kwargs) -> EndpointRoleExecutor:
    kwargs.setdefault("enabled", True)
    return EndpointRoleExecutor(url, **kwargs)


# -- L1: a real invocation behind COMPLETED --------------------------------
def test_executor_is_invoked_and_result_recorded(env, tmp_path):
    gate = PolicyGate(tmp_path / "state", activated=True)
    ledger = BudgetLedger(tmp_path / "state" / "budget")
    calls = []

    def executor(task, *, model, provider, workspace):
        calls.append(task.task_id)
        return {"done": True}

    orchestrator = Orchestrator(env, gate, ledger, provider=FakeResourceProvider(),
                                executor=executor)
    result = orchestrator.run_once()
    assert result["state"] == RunState.COMPLETED.value
    assert calls == ["audit"]
    assert result["result"] == {"done": True}


def test_executor_failure_marks_task_failed(env, tmp_path):
    gate = PolicyGate(tmp_path / "state", activated=True)
    ledger = BudgetLedger(tmp_path / "state" / "budget")

    def executor(task, *, model, provider, workspace):
        raise RuntimeError("boom")

    orchestrator = Orchestrator(env, gate, ledger, provider=FakeResourceProvider(),
                                executor=executor)
    result = orchestrator.run_once()
    assert result["state"] == RunState.FAILED.value
    assert "boom" in result["reason"]


def test_run_reaches_completed_only_through_the_real_endpoint(env, tmp_path, endpoint,
                                                              gate, ledger):
    orchestrator = Orchestrator(env, gate, ledger, provider=FakeResourceProvider(),
                                executor=_executor(endpoint.url))
    result = orchestrator.run_once()
    assert result["state"] == RunState.COMPLETED.value
    assert len(endpoint.requests) == 1
    request = endpoint.requests[0]
    assert request["path"] == "/invoke"
    assert request["payload"]["task"]["task_id"] == "audit"
    assert request["payload"]["role"] == "reviewer"
    assert request["payload"]["model"] == "ollama:qwen2.5-coder"
    assert request["payload"]["workspace"]["root"] == str(env.root)
    assert request["headers"]["X-Vertep-Contract"] == "role-executor/1"
    assert result["result"]["result"] == {"task_id": "audit"}
    records = orchestrator.results()
    assert len(records) == 1
    assert records[0]["role"] == "reviewer"
    assert records[0]["state"] == RunState.COMPLETED.value


def test_executor_token_is_sent_to_the_endpoint(env, tmp_path, endpoint, gate, ledger):
    executor = _executor(endpoint.url, token="lab-token")
    orchestrator = Orchestrator(env, gate, ledger, provider=FakeResourceProvider(),
                                executor=executor)
    orchestrator.run_once()
    assert endpoint.requests[0]["headers"]["X-Lab-Executor-Token"] == "lab-token"


# -- L2: fail-closed refusal paths -----------------------------------------
def test_disabled_executor_never_calls_the_endpoint(env, tmp_path, endpoint, gate,
                                                    ledger):
    executor = EndpointRoleExecutor(endpoint.url, enabled=False)
    orchestrator = Orchestrator(env, gate, ledger, provider=FakeResourceProvider(),
                                executor=executor)
    result = orchestrator.run_once()
    assert result["state"] == RunState.FAILED.value
    assert "disabled" in result["reason"]
    assert endpoint.requests == []
    records = orchestrator.results()
    assert len(records) == 1
    assert records[0]["state"] == RunState.FAILED.value
    assert records[0]["role"] == "reviewer"


def test_unreachable_executor_fails_the_run(env, tmp_path, gate, ledger):
    # Port 1 is reserved and never listens: the connection must be refused.
    executor = EndpointRoleExecutor("http://127.0.0.1:1", enabled=True, timeout=2)
    orchestrator = Orchestrator(env, gate, ledger, provider=FakeResourceProvider(),
                                executor=executor)
    result = orchestrator.run_once()
    assert result["state"] == RunState.FAILED.value
    # A refused connection and a timed-out one are both transport failures.
    assert "unreachable" in result["reason"] or "timed out" in result["reason"]


def test_endpoint_error_answer_fails_instead_of_completing(env, tmp_path, gate, ledger):
    service = _Endpoint(respond=lambda payload: {
        "status": "failed", "error": "model crashed"})
    try:
        orchestrator = Orchestrator(env, gate, ledger,
                                    provider=FakeResourceProvider(),
                                    executor=_executor(service.url))
        result = orchestrator.run_once()
    finally:
        service.close()
    assert result["state"] == RunState.FAILED.value
    assert "model crashed" in result["reason"]


def test_http_refusal_fails_instead_of_completing(env, tmp_path, gate, ledger):
    service = _Endpoint(status_code=403)
    try:
        orchestrator = Orchestrator(env, gate, ledger,
                                    provider=FakeResourceProvider(),
                                    executor=_executor(service.url))
        result = orchestrator.run_once()
    finally:
        service.close()
    assert result["state"] == RunState.FAILED.value
    assert "HTTP 403" in result["reason"]


def test_a_slow_endpoint_times_out_instead_of_completing(env, tmp_path, gate, ledger):
    service = _Endpoint(delay=2.0)
    try:
        orchestrator = Orchestrator(env, gate, ledger,
                                    provider=FakeResourceProvider(),
                                    executor=_executor(service.url, timeout=0.2))
        result = orchestrator.run_once()
    finally:
        service.close()
    assert result["state"] == RunState.FAILED.value
    assert "timed out" in result["reason"]


def test_paid_provider_is_refused_before_any_invocation(env, tmp_path, endpoint, gate,
                                                        ledger):
    orchestrator = Orchestrator(env, gate, ledger, provider=FakeResourceProvider(),
                                executor=_executor(endpoint.url))
    result = orchestrator.run_once(model="gpt-4o", provider="openai-paid")
    assert result["state"] == RunState.FAILED.value
    assert result["checks"] is None
    assert endpoint.requests == []


def test_executor_from_environment_needs_an_explicit_endpoint(monkeypatch):
    monkeypatch.delenv("LAB_ROLE_EXECUTOR_URL", raising=False)
    monkeypatch.delenv("LAB_ROLE_EXECUTOR_ENABLED", raising=False)
    assert executor_from_environment() is None
    monkeypatch.setenv("LAB_ROLE_EXECUTOR_URL", "http://runner:9000")
    disabled = executor_from_environment()
    assert disabled is not None and disabled.enabled is False
    monkeypatch.setenv("LAB_ROLE_EXECUTOR_ENABLED", "true")
    enabled = executor_from_environment()
    assert enabled is not None and enabled.enabled is True
    assert enabled.endpoint == "http://runner:9000"


def test_endpoint_executor_rejects_a_malformed_response():
    class _Response:
        def read(self):
            return b"not json"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    executor = EndpointRoleExecutor("http://runner:9000", enabled=True,
                                    opener=lambda request, timeout: _Response())
    with pytest.raises(ExecutorError):
        executor(None, model="", provider="", workspace=None)


def test_executor_requires_an_endpoint():
    executor = EndpointRoleExecutor("", enabled=True)
    with pytest.raises(ExecutorUnavailable):
        executor(None)


# -- L1/L8: role separation and isolation ----------------------------------
def test_reviewer_cannot_execute_change_work(env, tmp_path, endpoint, gate, ledger):
    orchestrator = Orchestrator(env, gate, ledger, provider=FakeResourceProvider(),
                                executor=_executor(endpoint.url))
    orchestrator.enqueue("fix", role="reviewer", payload={"patch": "x"})
    result = orchestrator.run_once()
    assert result["state"] == RunState.BLOCKED.value
    assert "role reviewer is not permitted to perform local_fix" in result["reason"]
    assert endpoint.requests == []
    records = orchestrator.results()
    assert records[0]["state"] == RunState.BLOCKED.value
    assert records[0]["role"] == "reviewer"


def test_qa_and_reviewer_keep_separate_results(env, tmp_path, endpoint, gate, ledger):
    orchestrator = Orchestrator(env, gate, ledger, provider=FakeResourceProvider(),
                                executor=_executor(endpoint.url))
    orchestrator.enqueue("test", role="qa", payload={"case": "1"})
    orchestrator.enqueue("audit", role="reviewer", payload={"scope": "gate"})
    first = orchestrator.run_once()
    second = orchestrator.run_once()
    assert first["state"] == RunState.COMPLETED.value
    assert second["state"] == RunState.COMPLETED.value
    records = orchestrator.results()
    assert [record["role"] for record in records] == ["qa", "reviewer"]
    assert [record["state"] for record in records] == ["COMPLETED", "COMPLETED"]
    assert records[0]["result"] != records[1]["result"]
    assert {request["payload"]["role"] for request in endpoint.requests} == {"qa",
                                                                            "reviewer"}


def test_non_isolated_workspace_stops_before_the_invocation(tmp_path, endpoint, gate,
                                                            ledger):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "lab").mkdir()
    (root / "tests").mkdir()
    (root / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n")
    workspace = Workspace(root, environ={"VERTEP_ROOT": "/opt/vertep"})
    orchestrator = Orchestrator(workspace, gate, ledger,
                                provider=FakeResourceProvider(),
                                executor=_executor(endpoint.url))
    result = orchestrator.run_once()
    assert result["state"] == RunState.BLOCKED.value
    assert result["reason"].startswith("isolation:")
    assert endpoint.requests == []


def test_executor_checks_isolation_itself(tmp_path, endpoint):
    root = tmp_path / "repo"
    root.mkdir()
    workspace = Workspace(root, environ={"VERTEP_ROOT": "/opt/vertep"})
    executor = _executor(endpoint.url)
    with pytest.raises(IsolationViolation):
        executor(object(), model="", provider="", workspace=workspace)
    assert endpoint.requests == []


def test_two_workspaces_keep_separate_results(tmp_path, endpoint, gate):
    roots = []
    for name in ("repo-a", "repo-b"):
        root = tmp_path / name
        root.mkdir()
        (root / "lab").mkdir()
        (root / "tests").mkdir()
        (root / "tests" / "test_smoke.py").write_text(
            "def test_smoke():\n    assert True\n")
        roots.append(root)

    ledger_a = BudgetLedger(tmp_path / "state" / "budget")
    orchestrator_a = Orchestrator(Workspace(roots[0], environ={}), gate, ledger_a,
                                  provider=FakeResourceProvider(),
                                  executor=_executor(endpoint.url))
    orchestrator_a.enqueue("test", role="qa")
    assert orchestrator_a.run_once()["state"] == RunState.COMPLETED.value

    orchestrator_b = Orchestrator(Workspace(roots[1], environ={}), gate,
                                  BudgetLedger(tmp_path / "state" / "budget"),
                                  provider=FakeResourceProvider(),
                                  executor=_executor(endpoint.url))
    orchestrator_b.enqueue("audit", role="reviewer")
    assert orchestrator_b.run_once()["state"] == RunState.COMPLETED.value

    assert [record["role"] for record in orchestrator_a.results()] == ["qa"]
    assert [record["role"] for record in orchestrator_b.results()] == ["reviewer"]
    assert orchestrator_a.queue[0].state == RunState.COMPLETED.value
    assert orchestrator_b.queue[0].state == RunState.COMPLETED.value
