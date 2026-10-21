"""Hybrid orchestrator proofs (Issue #107).

The orchestrator is the *software* half of the hybrid laboratory controller:
schedule, run and token limits, state and checkpoints, GPU availability,
permissions and timeouts.  These tests prove the key behaviour — cheap static
checks always precede any AI call, GPU is used only under a production lease,
and a restart restores state without losing the hand-off.
"""

import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from lab.budget import BudgetLedger
from lab.gpu_lease import FakeResourceProvider
from lab.orchestrator import Orchestrator, RunState, StaticChecks, Task
from lab.policy import PolicyGate
from lab.workspace import Workspace


@pytest.fixture()
def environment(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# lab", encoding="utf-8")
    (root / "lab").mkdir()
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_smoke.py").write_text("def test_smoke():\n    assert True\n",
                                         encoding="utf-8")
    # The isolated lab VM runs with a clean environment; the enclosing Vertep
    # test process sets production variables, so inject the lab's own view.
    return Workspace(root, environ={})


@pytest.fixture()
def gate(tmp_path):
    return PolicyGate(tmp_path / "state", activated=True)


@pytest.fixture()
def ledger(tmp_path):
    return BudgetLedger(tmp_path / "state")


def _stub_executor(task, *, model, provider, workspace):
    """Minimal real invocation stand-in for the controlled role endpoint."""
    return {"task_id": task.task_id, "role": task.role}


@pytest.fixture()
def orchestrator(environment, gate, ledger):
    return Orchestrator(environment, gate, ledger, provider=FakeResourceProvider(),
                        executor=_stub_executor)


def test_orchestrator_starts_with_an_empty_queue(orchestrator):
    assert orchestrator.queue == []


def test_enqueue_rejects_unknown_roles(environment, gate, ledger):
    with pytest.raises(ValueError):
        Orchestrator(environment, gate, ledger).enqueue("fix", role="cctv")


def test_static_checks_are_run_before_any_ai_call(environment, gate, ledger, tmp_path):
    provider = FakeResourceProvider()
    orchestrator = Orchestrator(environment, gate, ledger, provider=provider,
                                actor="lab", executor=_stub_executor)
    result = orchestrator.run_once()
    assert result["state"] == RunState.COMPLETED.value
    assert result["checks"]["passed"] is True
    assert "role reviewer" in result["reason"]
    assert "role executor" in result["reason"]


def test_run_without_an_executor_is_blocked_not_completed(environment, gate, ledger,
                                                          tmp_path):
    orchestrator = Orchestrator(environment, gate, ledger,
                                provider=FakeResourceProvider())
    result = orchestrator.run_once()
    assert result["state"] == RunState.BLOCKED.value
    assert "no role executor configured" in result["reason"]
    assert orchestrator.results() == []


def test_failed_static_checks_stop_before_model_calls(environment, gate, ledger, tmp_path):
    (environment.root / "core").mkdir()
    (environment.root / "core" / "x.py").write_text("syntax error", encoding="utf-8")
    result = Orchestrator(environment, gate, ledger, provider=FakeResourceProvider()).run_once()
    assert result["state"] == RunState.FAILED.value
    assert "before any AI call" in result["reason"]


def test_rate_limit_pauses_the_laboratory(environment, gate, ledger, tmp_path):
    ledger.max_runs_per_hour = 0
    ledger.pause(3600, reason="rate limit")
    result = Orchestrator(environment, gate, ledger).run_once()
    assert result["state"] == RunState.PAUSED.value


def test_run_is_blocked_when_the_lease_cannot_be_granted(environment, gate, ledger, tmp_path):
    provider = FakeResourceProvider(queue_depth=1)
    result = Orchestrator(environment, gate, ledger, provider=provider,
                          executor=_stub_executor).run_once()
    assert result["state"] == RunState.BLOCKED.value
    assert "lease" in result["reason"]


def test_restart_recovers_state_and_preserves_handoff(environment, gate, ledger, tmp_path):
    work = tmp_path / "workspace"
    shutil.copytree(environment.root, work)
    first = Orchestrator(Workspace(work, environ={}), gate, ledger,
                         provider=FakeResourceProvider(), executor=_stub_executor)
    first.enqueue("audit", role="reviewer", payload={"scope": "gate"})
    outcome = first.run_once()
    assert outcome["state"] == RunState.COMPLETED.value

    second = Orchestrator(Workspace(work, environ={}), gate, ledger,
                          provider=FakeResourceProvider(), executor=_stub_executor)
    assert len(second.queue) == 1
    assert second.queue[0].state == RunState.COMPLETED.value
    handoff = second.read_handoff()
    assert handoff.get("summary")
    assert handoff.get("pending_tasks") == []
    # The second orchestrator resumes the same isolated copy without re-reading
    # the repository from scratch.
    assert work.joinpath(".lab", "handoff.json").is_file()


def test_restart_preserves_budget_and_queue(environment, gate, ledger, tmp_path):
    work = tmp_path / "workspace"
    shutil.copytree(environment.root, work)
    first = Orchestrator(Workspace(work, environ={}), gate, ledger,
                         provider=FakeResourceProvider(), executor=_stub_executor)
    first.enqueue("test", role="qa", payload={"case": "1"})
    first.run_once()

    second = Orchestrator(Workspace(work, environ={}), gate, ledger,
                          provider=FakeResourceProvider())
    assert len(second.queue) == 1
    assert second.queue[0].payload == {"case": "1"}


def test_run_cycle_stops_on_the_first_blocking_result(environment, gate, ledger, tmp_path):
    (environment.root / "core").mkdir()
    (environment.root / "core" / "broken.py").write_text("x = )", encoding="utf-8")
    outcomes = Orchestrator(environment, gate, ledger,
                            provider=FakeResourceProvider()).run_cycle(max_runs=3)
    assert outcomes[0]["state"] == RunState.FAILED.value
    assert all(item["state"] == RunState.FAILED.value for item in outcomes)


def test_an_expired_run_is_blocked_before_any_role_invocation(environment, gate, ledger,
                                                              tmp_path):
    calls = []

    def executor(task, *, model, provider, workspace):
        calls.append(task.task_id)
        return {"task_id": task.task_id}

    orchestrator = Orchestrator(environment, gate, ledger,
                                provider=FakeResourceProvider(), executor=executor)
    # The cycle budget is already spent: the run must stop, not execute.
    orchestrator.started = time.monotonic() - 3600
    result = orchestrator.run_once()
    assert result["state"] == RunState.BLOCKED.value
    assert result["reason"] == "run timeout"
    assert calls == []


def test_status_maps_the_gpu_and_budget_state(environment, gate, ledger, tmp_path):
    leases = FakeResourceProvider(vram_mb=8192)
    orchestrator = Orchestrator(environment, gate, ledger, provider=leases)
    status = orchestrator.status()
    assert status["workspace"]["root"].endswith("repo")
    assert status["budget"]["max_runs_per_day"] == 24
    assert status["roles"] == ["architect", "backend", "ai_gpu", "frontend",
                               "devops", "qa", "security", "reviewer"]
    assert status["executor"] == {"configured": False}
