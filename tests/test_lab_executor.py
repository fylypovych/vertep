"""Role executor invocation in the laboratory orchestrator (Issue i.0.0.2.50, L1)."""

import tempfile
from pathlib import Path

import pytest

from lab.budget import BudgetLedger
from lab.gpu_lease import FakeResourceProvider
from lab.orchestrator import Orchestrator, RunState
from lab.policy import PolicyGate
from lab.workspace import Workspace


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "lab").mkdir()
    (root / "tests").mkdir()
    (root / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n")
    return Workspace(root, environ={})


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