"""#387 F3: a Telegram update operation follows the real executor route.

The operation is linked to a request that the real update agent executes against
the disposable host consumer with a signed manifest.  Reconciliation and the
pending Telegram notification are produced by *separate* Python processes, i.e.
after CORE has been restarted, so the phase/terminal state must come from the
durable executor and operation records alone.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECONCILE = ("from core.telegram.handlers import _reconcile_update_operations; "
             "_reconcile_update_operations()")


def _agent():
    spec = importlib.util.spec_from_file_location("update_agent_lifecycle", ROOT / "scripts" / "update-agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _package(base: Path, version: str) -> Path:
    payload = base / f"payload-{version}"
    payload.mkdir(parents=True)
    (payload / "VERSION").write_text(version, encoding="utf-8")
    archive = base / f"vertep-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        package.add(payload, arcname=payload.name)
    return archive


def _restarted_core(code: str) -> None:
    """Run ``code`` in a fresh CORE process that shares only durable state."""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=dict(os.environ),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def _operation(operation_id: str) -> dict:
    from core.operations import get_operation
    return get_operation(operation_id)


def _queue(operation_type_target: str):
    from core.operations import begin_operation, create_operation, link_operation_request
    from core.update_manager import request_update

    operation = create_operation("update", "telegram:42", target=operation_type_target)
    begin_operation(operation["operation_id"], "Оновлення")
    queued = request_update("update")
    link_operation_request(operation["operation_id"], queued)
    return operation["operation_id"], queued["request_id"]


def test_telegram_update_operation_reaches_terminal_states_across_core_restarts(
        disposable_deployment, signed_update_server, tmp_path, monkeypatch):
    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "update-state"
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
    agent = _agent()
    from core.operations import pending_notifications

    # 1) error: a tampered release fails before mutation; a restarted CORE
    #    reports the correlated failure to the requesting chat.
    signed_update_server.publish("0.0.0.2", _package(tmp_path / "bad", "0.0.0.2"), tamper=True)
    failed_id, failed_request = _queue("0.0.0.2")
    _restarted_core(RECONCILE)
    assert _operation(failed_id)["current_phase"] == "CHECKING"
    assert agent.process_request(root, state_dir, state_dir / "requests" / f"{failed_request}.json",
                                 skip_drain=True) is False
    _restarted_core(RECONCILE)
    failed = _operation(failed_id)
    assert failed["status"] == "FAILED" and "signature" in failed["error"].lower()
    assert (root / "current" / "VERSION").read_text(encoding="utf-8").strip() == "0.0.0.1"

    # 2) retry: a valid signed release succeeds, again observed by a restarted CORE.
    signed_update_server.publish("0.0.0.2", _package(tmp_path / "good", "0.0.0.2"))
    retry_id, retry_request = _queue("0.0.0.2")
    assert retry_request != failed_request
    assert agent.process_request(root, state_dir, state_dir / "requests" / f"{retry_request}.json",
                                 skip_drain=True) is True
    _restarted_core(RECONCILE)
    completed = _operation(retry_id)
    assert completed["status"] == "COMPLETED"
    assert completed["result"]["request_id"] == retry_request
    assert (root / "current" / "VERSION").read_text(encoding="utf-8").strip() == "0.0.0.2"

    # 3) both outcomes are waiting for the same chat, each bound to its operation.
    messages = [item["message"] for item in pending_notifications() if item["chat_id"] == "42"]
    assert any(failed_id[:8] in message and message.startswith("❌") for message in messages)
    assert any(retry_id[:8] in message and message.startswith("✅") for message in messages)

    # 4) another restart neither repeats nor rewrites the terminal records.
    _restarted_core(RECONCILE)
    assert _operation(retry_id)["status"] == "COMPLETED"
    assert _operation(failed_id)["status"] == "FAILED"
    assert len([item for item in pending_notifications() if item["chat_id"] == "42"]) == len(messages)
    assert json.loads((state_dir / "status.json").read_text(encoding="utf-8"))["request_id"] == retry_request
