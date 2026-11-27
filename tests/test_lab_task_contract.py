"""Tests for the mandatory 4-point LAB Task Contract (template + fail-closed enforcement)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab.api import create_app
from lab.budget import BudgetLedger
from lab.cli import main
from lab.orchestrator import Orchestrator, RunState
from lab.policy import PolicyGate
from lab.task_contract import (
    TASK_CONTRACT_FORMAT,
    TaskContractError,
    load_task_contract_template,
    requires_task_contract,
    validate_task_contract,
)
from lab.workspace import FORBIDDEN_ENVIRONMENT, Workspace
from tests.lab_admin import AdminClient as TestClient, vertep_identity  # noqa: F401


VALID_MARKDOWN = """# Шаблон задачі лабораторії (Task Contract)

## 1. Що змінюємо (файли)
- `lab/orchestrator.py`
- `tests/test_lab_task_contract.py`

## 2. Які інваріанти не можна зламати
- Ізоляція LAB (#491) і PolicyGate fail-closed
- Ревізор залишається read-only без права `local_fix`

## 3. Як перевіряємо без GPU
- `.venv/bin/python -m compileall -q lab tests`
- `.venv/bin/python -m pytest -q tests/test_lab_task_contract.py tests/test_lab_orchestrator.py`

## 4. Який артефакт буде доказом
- Вивід pytest + запис `state/results.jsonl` із SHA256 контракту
"""


VALID_DICT = {
    "files_to_change": ["lab/orchestrator.py", "tests/test_lab_task_contract.py"],
    "invariants": ["Ізоляція LAB (#491) і PolicyGate fail-closed"],
    "no_gpu_verification": [".venv/bin/python -m pytest -q tests/test_lab_task_contract.py"],
    "proof_artifact": "Вивід pytest + запис state/results.jsonl",
}


def test_template_exists_has_all_four_sections_and_unfilled_is_rejected():
    template = load_task_contract_template()
    for heading in (
        "Що змінюємо (файли)",
        "Які інваріанти не можна зламати",
        "Як перевіряємо без GPU",
        "Який артефакт буде доказом",
    ):
        assert heading in template
    # The raw unfilled template contains placeholders and must fail closed.
    with pytest.raises(TaskContractError):
        validate_task_contract(template)


def test_markdown_and_dict_validation_and_fail_closed_checks():
    parsed = validate_task_contract(VALID_MARKDOWN)
    assert parsed.files_to_change == ("lab/orchestrator.py", "tests/test_lab_task_contract.py")
    assert len(parsed.invariants) == 2
    assert len(parsed.no_gpu_verification) == 2
    assert "results.jsonl" in parsed.proof_artifact
    as_dict = parsed.as_dict()
    assert as_dict["format"] == TASK_CONTRACT_FORMAT
    assert len(as_dict["sha256"]) == 64

    # Missing any of the 4 fields fails closed.
    for key in ("files_to_change", "invariants", "no_gpu_verification", "proof_artifact"):
        incomplete = {k: v for k, v in VALID_DICT.items() if k != key}
        with pytest.raises(TaskContractError, match=key):
            validate_task_contract(incomplete)

    # Placeholders and unsafe paths fail closed.
    with pytest.raises(TaskContractError):
        validate_task_contract({**VALID_DICT, "invariants": ["TBD"]})
    with pytest.raises(TaskContractError, match="repository-relative"):
        validate_task_contract({**VALID_DICT, "files_to_change": ["/etc/passwd"]})
    with pytest.raises(TaskContractError, match="unsafe path"):
        validate_task_contract({**VALID_DICT, "files_to_change": ["../core/app.py"]})

    assert requires_task_contract("fix") is True
    assert requires_task_contract("implement") is True
    assert requires_task_contract("audit") is False
    assert requires_task_contract("review") is False
    assert requires_task_contract("test") is False


def _make_workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    (root / "lab").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "README.md").write_text("# lab\n", encoding="utf-8")
    (root / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    return Workspace(root, environ={})


def test_orchestrator_blocks_coding_task_without_task_contract_before_executor(tmp_path):
    workspace = _make_workspace(tmp_path)
    gate = PolicyGate(tmp_path / "state", activated=True)
    budget = BudgetLedger(tmp_path / "state" / "budget")
    calls: list = []

    def executor(task, *, model, provider, workspace):
        calls.append(task.as_dict())
        return {"task_id": task.task_id, "role": task.role}

    orch = Orchestrator(workspace, gate, budget, executor=executor)
    task = orch.enqueue("fix", role="backend", task_id="no-contract", payload={"detail": "edit code"})
    # Even if an owner grant exists, missing Task Contract blocks execution before coding starts.
    gate.register_grant(
        actor="lab",
        action="execute_approved_task",
        subject=task.task_id,
        scope_version=task.approval_scope,
        approved_by="owner",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    outcome = orch.run_once()
    assert outcome["state"] == RunState.BLOCKED.value
    assert "Task Contract" in outcome["reason"]
    assert calls == []

    denied = [r for r in gate.trail.records() if r.get("action") == "task_contract"]
    assert denied and denied[-1]["decision"] == "DENIED"
    assert denied[-1]["rule"] == "task_contract_required"

    # Now enqueue with a valid Task Contract and matching grant -> executor receives locked contract.
    valid_task = orch.enqueue(
        "fix",
        role="backend",
        task_id="with-contract",
        payload={"detail": "edit code"},
        task_contract=VALID_MARKDOWN,
    )
    gate.register_grant(
        actor="lab",
        action="execute_approved_task",
        subject=valid_task.task_id,
        scope_version=valid_task.approval_scope,
        approved_by="owner",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    ok = orch.run_once()
    assert ok["state"] == RunState.COMPLETED.value
    assert len(calls) == 1
    assert calls[0]["payload"]["task_contract"]["format"] == TASK_CONTRACT_FORMAT
    assert calls[0]["payload"]["task_contract"]["files_to_change"] == [
        "lab/orchestrator.py",
        "tests/test_lab_task_contract.py",
    ]


def test_cli_contract_and_enqueue_enforcement(tmp_path, monkeypatch, capsys):
    for name in FORBIDDEN_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LAB_POLICY_GATE_ACTIVE", "true")
    state = tmp_path / "state"
    ws = _make_workspace(tmp_path).root
    base = ["--state-dir", str(state), "--workspace", str(ws)]

    assert main([*base, "contract", "template"]) == 0
    tpl_out = json.loads(capsys.readouterr().out)
    assert tpl_out["format"] == TASK_CONTRACT_FORMAT
    assert len(tpl_out["required_fields"]) == 4

    bad_file = tmp_path / "bad.md"
    bad_file.write_text(tpl_out["template_markdown"], encoding="utf-8")
    assert main([*base, "contract", "validate", str(bad_file)]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False

    good_file = tmp_path / "good.md"
    good_file.write_text(VALID_MARKDOWN, encoding="utf-8")
    assert main([*base, "contract", "validate", str(good_file)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    # Enqueueing a code-change task without --contract is refused fail-closed.
    assert main([*base, "enqueue", "fix", "--role", "backend", "--detail", "patch"]) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused["enqueued"] is False
    assert "Task Contract" in refused["reason"]

    # Enqueueing with --contract succeeds and embeds normalized contract.
    assert main([*base, "enqueue", "fix", "--role", "backend", "--contract", str(good_file)]) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["payload"]["task_contract"]["format"] == TASK_CONTRACT_FORMAT


def test_api_task_contract_endpoints_and_enqueue_gate(tmp_path, vertep_identity):
    client = TestClient(create_app(state_dir=tmp_path / "state", workspace_root=tmp_path / "repo"))

    # Anonymous access is refused (consistent with #491 L2).
    assert client.get("/api/lab/task-contract/template", headers={}).status_code == 401

    tpl = client.get("/api/lab/task-contract/template")
    assert tpl.status_code == 200
    assert "Що змінюємо (файли)" in tpl.json()["template_markdown"]

    bad_val = client.post("/api/lab/task-contract/validate", json={"contract": tpl.json()["template_markdown"]})
    assert bad_val.status_code == 422

    ok_val = client.post("/api/lab/task-contract/validate", json={"contract": VALID_MARKDOWN})
    assert ok_val.status_code == 200
    assert ok_val.json()["valid"] is True

    # Change task without task_contract -> 422 fail-closed.
    no_contract = client.post("/api/lab/tasks", json={"kind": "fix", "role": "backend", "payload": {}})
    assert no_contract.status_code == 422

    # Read-only review task does not require code-change contract.
    review = client.post("/api/lab/tasks", json={"kind": "review", "role": "reviewer", "payload": {}})
    assert review.status_code == 200

    # Change task with filled Markdown task_contract -> 200 and normalized contract stored.
    with_contract = client.post(
        "/api/lab/tasks",
        json={"kind": "fix", "role": "backend", "payload": {}, "task_contract": VALID_MARKDOWN},
    )
    assert with_contract.status_code == 200
    assert with_contract.json()["task"]["payload"]["task_contract"]["format"] == TASK_CONTRACT_FORMAT
