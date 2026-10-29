"""Configured Telegram choice, revision fencing, and versioned worker lifecycle."""

from datetime import datetime, timezone, timedelta
import json
from unittest.mock import Mock

import pytest

from core.protocol.task import TaskClaim, TaskRequest, TaskComplete, TaskCancel
from worker.protocol.handler import WorkerProtocolHandler


def test_worker_version_ownership_and_core_only_renewal():
    worker = WorkerProtocolHandler("worker")
    worker.capabilities = {"text_generation"}
    request = TaskRequest("task", "job", "text", "text_generation", {}, vram_mb=0, lease_id="current")
    assert worker.handle_task_assignment(request).accepted
    initial = worker.current_task.lease_expires_at
    worker.handle_heartbeat({})
    assert worker.current_task.lease_expires_at == initial
    assert not worker.handle_task_assignment(request).accepted
    worker.handle_task_complete(TaskComplete("task", "worker", lease_id="old"))
    assert worker.current_task is not None
    worker.handle_task_cancel(TaskCancel("task", "core", lease_id="old"))
    assert worker.current_task is not None
    renewal = (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()
    worker.handle_heartbeat({"lease_id": "current", "lease_expires_at": renewal})
    assert worker.current_task.lease_expires_at.isoformat() == renewal
    worker.handle_task_complete(TaskComplete("task", "worker", lease_id="current"))
    assert worker.current_task is None
    request.protocol_version = "2.0.0"
    assert not worker.handle_task_assignment(request).accepted
    assert TaskClaim("x", "worker").claimed_at.tzinfo is not None


def test_telegram_choice_precedes_execution_and_is_idempotent(tmp_path, monkeypatch):
    import core.telegram.workflows as telegram
    from core.workflow.runtime import workflow_runtime
    root = tmp_path / "templates"
    root.mkdir()
    (root / "standard.json").write_text(json.dumps({"name": "Стандартний", "tasks": [{"id": "script", "type": "text_generation"}]}), encoding="utf-8")
    monkeypatch.setenv("WORKFLOW_DEFINITIONS_ROOT", str(root))
    monkeypatch.setenv("WORKFLOW_STATE_ROOT", str(tmp_path / "state"))
    adapter = Mock()
    monkeypatch.setattr(telegram, "TelegramAdapter", lambda: adapter)
    pending = {"pending": {"text": "topic"}, "brand_id": "brand01"}
    offered = telegram.offer_workflows("chat", pending, "character")
    assert offered["status"] == "workflow_selection"
    runtime = workflow_runtime()
    assert runtime.engine._jobs == {}
    selected = telegram.select_workflow({"id": "choice"}, "chat", "0", pending)
    assert selected["status"] == "IN_PROGRESS"
    assert len(runtime.engine._jobs) == 1
    repeat = telegram.select_workflow({"id": "choice"}, "chat", "0", pending)
    assert repeat["job_id"] == selected["job_id"]
    assert len(runtime.engine._jobs) == 1
    with pytest.raises(ValueError, match="чату"):
        telegram.handle_action({}, "other-chat", "c:" + selected["job_id"])
    cancelled = telegram.handle_action({}, "chat", "c:" + selected["job_id"])
    assert cancelled["status"] == "CANCELLED"


def test_telegram_revision_reexecutes_and_rejects_old_approval(tmp_path, monkeypatch):
    import core.telegram.workflows as telegram
    from core.workflow.runtime import workflow_runtime
    from core.state import store
    root = tmp_path / "templates"
    root.mkdir()
    (root / "standard.json").write_text(json.dumps({"name": "Стандартний", "tasks": [{"id": "script", "type": "text_generation", "approval_required": True}]}))
    monkeypatch.setenv("WORKFLOW_DEFINITIONS_ROOT", str(root))
    monkeypatch.setenv("WORKFLOW_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(telegram, "TelegramAdapter", lambda: Mock())
    pending = {"pending": {"text": "topic"}, "brand_id": "brand01"}
    telegram.offer_workflows("chat", pending, "character")
    selected = telegram.select_workflow({}, "chat", "0", pending)
    runtime = workflow_runtime()
    now = datetime.now(timezone.utc).isoformat()
    worker = {"node_name": "worker", "role": "text", "status": "READY", "last_seen": now,
              "capabilities": ["text_generation"], "tested_capabilities": ["text_generation"],
              "self_test": {"status": "PASSED", "role": "text", "checked_at": now}}
    store.workers["worker"] = worker
    transport = runtime.register(worker)
    runtime.tick()
    first = transport.poll()
    assert first is not None
    transport.result(first["lease_id"], [{"text": "first external output"}])
    runtime.tick()
    job = selected["job_id"]
    telegram.handle_action({}, "chat", f"e:{job}:0:1")
    telegram.apply_revision("chat", "Змінити завершення")
    second = transport.poll()
    assert second["lease_id"] != first["lease_id"]
    assert second["revision"] == "Змінити завершення"
    transport.result(second["lease_id"], [{"text": "revised external output"}])
    runtime.tick()
    with pytest.raises(ValueError, match="Stale"):
        telegram.handle_action({}, "chat", f"a:{job}:0:1")
    final = telegram.handle_action({}, "chat", f"a:{job}:0:2")
    assert final["status"] == "COMPLETED"
    assert final["tasks"]["script"]["output"] == [{"text": "revised external output"}]
