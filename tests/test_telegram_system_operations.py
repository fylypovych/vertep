"""Tests for Telegram system operations: Status, Update, Restart, Backup, Restore, Test."""

import core.api.system as _owner_core_api_system
import core.operations as _owner_core_operations
import core.system_state as _owner_core_system_state
import core.telegram.handlers as _owner_core_telegram_handlers
import asyncio
import importlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from unittest.mock import Mock, patch


class TestSystemMenu:
    def test_system_menu_admin(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers.store, "jobs", {})
        msg = {"text": "/system", "chat": {"id": "42"}, "message_id": 1}
        result = _owner_core_telegram_handlers._handle_telegram_message("42", "1", "/system", msg)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_status:menu" in all_callbacks
        assert "sys_update:menu" in all_callbacks
        assert "sys_restart:menu" in all_callbacks
        assert "sys_backup:menu" in all_callbacks
        assert "sys_restore:menu" in all_callbacks
        assert "sys_test:menu" in all_callbacks

    def test_system_menu_non_admin(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: False)
        msg = {"text": "/system", "chat": {"id": "99"}, "message_id": 1}
        _owner_core_telegram_handlers._handle_telegram_message("99", "1", "/system", msg)
        assert "Доступ заборонено" in adapter.send_message.call_args[0][1]


class TestStatusOperation:
    def test_status_callback_returns_text(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "list_operations", lambda n: [])
        monkeypatch.setattr(_owner_core_telegram_handlers, "update_status", lambda: {"current_version": "0.0.1.1"})
        monkeypatch.setattr(_owner_core_telegram_handlers.store, "jobs", {})
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_system_state", lambda: {"state": "NORMAL"})
        monkeypatch.setattr(_owner_core_telegram_handlers, "_sync_internal_api", lambda *a, **kw: None)
        cb = {"id": "cb-status", "data": "sys_status:menu", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "Система:" in text or "Нормальний" in text or "NORMAL" in text or "IDLE" in text


class TestCreateOperation:
    def test_create_operation_returns_record(self, monkeypatch):
        module = importlib.import_module("core.operations")
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("UPDATE_STATE_DIR", tmpdir)
            op = _owner_core_operations.create_operation("status", "test_user")
            assert isinstance(op, dict)
            assert "operation_id" in op
            assert len(op["operation_id"]) == 32
            assert op["type"] == "status"
            assert op["requested_by"] == "test_user"
            assert op["status"] == "QUEUED"

    def test_audit_entry_is_durable_and_contains_operation_context(self, monkeypatch, tmp_path):
        module = importlib.import_module("core.operations")
        monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
        op = _owner_core_operations.create_operation("restart", "telegram:42", target="worker-1")
        _owner_core_telegram_handlers.begin_operation(op["operation_id"], "restart requested")
        entry = _owner_core_telegram_handlers.audit_entry(op["operation_id"], "HOST_REQUEST", "restart-runtime")

        persisted = json.loads(
            (tmp_path / f"{op['operation_id']}.audit.jsonl").read_text(encoding="utf-8")
        )
        assert persisted == entry
        assert entry["operation_type"] == "restart"
        assert entry["target"] == "worker-1"
        assert entry["status"] == "RUNNING"


class TestInternalCoreCall:
    def test_authenticated_internal_call_reaches_disposable_service(self, monkeypatch):
        module = importlib.import_module("core.app")
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received["key"] = self.headers.get("x-vertep-internal-key")
                received["body"] = json.loads(self.rfile.read(
                    int(self.headers.get("content-length", "0"))))
                if received["key"] != "acceptance-key":
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps({"status": "accepted"}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for variable in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
                             "HTTPS_PROXY", "https_proxy"):
                monkeypatch.delenv(variable, raising=False)
            monkeypatch.setenv("CORE_API_URL", f"http://127.0.0.1:{server.server_port}")
            monkeypatch.setenv("INTERNAL_API_KEY", "acceptance-key")
            result = _owner_core_telegram_handlers._call_core_api("POST", "/api/system/test", {"scope": "quick"})
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        assert result == {"status": "accepted"}
        assert received == {"key": "acceptance-key", "body": {"scope": "quick"}}


class TestUpdateOperation:
    def test_update_menu_shows_confirmation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "update_status", lambda: {"current_version": "0.0.1.1"})
        cb = {"id": "cb-upd-1", "data": "sys_update:menu", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_update_check" in all_callbacks

    def test_update_menu_offers_apply_when_version_is_available(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "_reconcile_update_operations", lambda: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "update_status", lambda: {
            "current_version": "0.0.1.1", "available_version": "0.0.1.2"})
        cb = {"id": "cb-upd-available", "data": "sys_update:menu",
              "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        assert "sys_update_confirm" in callbacks

    def test_update_idempotency_blocks_duplicate(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress",
                            lambda t: {"operation_id": "abc12345"} if t == "update" else None)
        cb = {"id": "cb-upd-2", "data": "sys_update:menu", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        text = adapter.answer_callback.call_args[0][1]
        assert "вже в процесі" in text

    def test_update_check_idempotency_blocks_duplicate(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
        # Mock get_or_create_operation to return existing operation (created=False)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: ({"operation_id": "chk12345", "type": "update"}, False))
        cb = {"id": "cb-upd-chk", "data": "sys_update_check", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "вже в процесі" in text or "процесі" in text

    def test_update_confirm_idempotency_blocks_duplicate(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: ({"operation_id": "cnf12345", "type": "update"}, False))
        cb = {"id": "cb-upd-cnf", "data": "sys_update_confirm", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "вже в процесі" in text or "процесі" in text

    def test_update_check_creates_operation_before_request(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        call_order = []
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "request_update", lambda a: call_order.append(("request_update", a)))
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: call_order.append(("get_or_create", a, kw)) or ({"operation_id": "x" * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "begin_operation", lambda *a, **kw: call_order.append(("begin", a)))
        cb = {"id": "cb-order", "data": "sys_update_check", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        # get_or_create_operation must be called before request_update
        ops = [c[0] for c in call_order]
        assert ops.index("get_or_create") < ops.index("request_update")

    def test_update_result_reconciles_and_queues_delivery(self, monkeypatch, tmp_path):
        module = importlib.import_module("core.app")
        operations = importlib.import_module("core.operations")
        monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
        op = operations.create_operation("update", "telegram:42", target="check")
        operations.begin_operation(op["operation_id"], "Перевірка")
        operations.link_operation_request(op["operation_id"], {
            "request_id": "a" * 32, "action": "check"})
        monkeypatch.setattr(_owner_core_telegram_handlers, "update_status", lambda: {
            "request_id": "a" * 32, "state": "SUCCEEDED",
            "message": "Update check completed", "available_version": "0.0.2.0"})
        _owner_core_telegram_handlers._reconcile_update_operations()
        finished = operations.get_operation(op["operation_id"])
        assert finished["status"] == "COMPLETED"
        notifications = operations.pending_notifications()
        assert notifications[0]["chat_id"] == "42"
        assert op["operation_id"][:8] in notifications[0]["message"]

    def test_pending_delivery_is_acknowledged_only_after_send(self, monkeypatch, tmp_path):
        module = importlib.import_module("core.app")
        operations = importlib.import_module("core.operations")
        monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
        operations.add_pending_notification("42", "done")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        _owner_core_telegram_handlers._deliver_pending_telegram_notifications()
        adapter.send_message.assert_called_once_with("42", "done")
        assert operations.pending_notifications() == []


class TestRestartOperation:
    @pytest.mark.skipif(os.name != "posix",
                        reason="stub systemctl is a shell script resolved through a POSIX PATH")
    def test_core_restart_executes_disposable_host_command(self, monkeypatch, tmp_path):
        module = importlib.import_module("core.app")
        binary_dir = tmp_path / "bin"
        binary_dir.mkdir()
        effect = tmp_path / "systemctl-effect"
        systemctl = binary_dir / "systemctl"
        systemctl.write_text(
            "#!/bin/bash\nprintf '%s' \"$*\" > \"$VERTEP_TEST_EFFECT\"\n",
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ.get('PATH', '')}")
        monkeypatch.setenv("VERTEP_TEST_EFFECT", str(effect))
        monkeypatch.setattr(_owner_core_system_state, "set_system_state", lambda *args, **kwargs: None)

        result = asyncio.run(_owner_core_api_system.system_restart({"target": "core"}))

        assert result["status"] == "restart_requested"
        assert result["previous_runtime_instance_id"] == _owner_core_telegram_handlers.CORE_RUNTIME_INSTANCE_ID
        assert effect.read_text(encoding="utf-8") == "restart vertep-core.service"

    def test_restart_menu_shows_core_worker(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        cb = {"id": "cb-restart", "data": "sys_restart:menu", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_restart_core" in all_callbacks
        assert "sys_restart_worker" in all_callbacks

    def test_restart_core_confirmation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        cb = {"id": "cb-rcore", "data": "sys_restart_core", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_restart_core_confirm" in all_callbacks

    def test_restart_core_execute(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: ({"operation_id": "a" * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "advance_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "complete_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "fail_operation", lambda *a, **kw: None)
        # First call returns restart_requested, subsequent calls return healthy
        call_count = {"count": 0}
        def mock_call_api(method, path, payload=None, timeout=30):
            call_count["count"] += 1
            if path == "/api/system/restart":
                return {"status": "restart_requested", "previous_runtime_instance_id": "core-old"}
            if path == "/api/health":
                return ({"status": "healthy", "runtime_instance_id": "core-new"}
                        if call_count["count"] > 1 else {"status": "starting"})
        monkeypatch.setattr(_owner_core_telegram_handlers, "_call_core_api", mock_call_api)
        import time
        monkeypatch.setattr(time, "sleep", lambda s: None)  # speed up test
        cb = {"id": "cb-rcore-run", "data": "sys_restart_core_confirm", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "HEALTHY" in text or "healthy" in text.lower() or "успішно" in text.lower()

    def test_restart_core_timeout_fails(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: ({"operation_id": "t" * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "advance_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "fail_operation", lambda *a, **kw: None)
        def mock_call_api(method, path, payload=None, timeout=30):
            if path == "/api/health":
                return {"status": "healthy", "runtime_instance_id": "core-old"}
            return {"status": "restart_requested", "previous_runtime_instance_id": "core-old"}
        monkeypatch.setattr(_owner_core_telegram_handlers, "_call_core_api", mock_call_api)
        import time
        monkeypatch.setattr(time, "sleep", lambda s: None)
        cb = {"id": "cb-rcore-timeout", "data": "sys_restart_core_confirm", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "не став" in text or "timeout" in text.lower() or "60s" in text

    def test_restart_worker_execute_waits_ready(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: ({"operation_id": "w" * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "advance_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "complete_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "fail_operation", lambda *a, **kw: None)
        call_count = {"count": 0}
        def mock_call_api(method, path, payload=None, timeout=30):
            call_count["count"] += 1
            if path == "/api/nodes/gpu-01/actions":
                return {"status": "restart_requested", "restart_operation_id": "restart-1"}
            if path == "/api/nodes/gpu-01":
                return ({"status": "READY", "update_state": {
                    "restart_ack": {"operation_id": "restart-1"}}}
                        if call_count["count"] > 1 else {"status": "UPDATING"})
        monkeypatch.setattr(_owner_core_telegram_handlers, "_call_core_api", mock_call_api)
        import time
        monkeypatch.setattr(time, "sleep", lambda s: None)
        cb = {"id": "cb-rwc", "data": "sys_restart_worker_confirm:gpu-01", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "READY" in text or "ready" in text.lower() or "успішно" in text.lower()

    def test_restart_worker_timeout_fails(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: ({"operation_id": "w" * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "advance_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "fail_operation", lambda *a, **kw: None)
        def mock_call_api(method, path, payload=None, timeout=30):
            if path == "/api/nodes/gpu-01":
                return {"status": "READY", "update_state": {
                    "restart_ack": {"operation_id": "different-request"}}}
            return {"status": "restart_requested", "restart_operation_id": "restart-1"}
        monkeypatch.setattr(_owner_core_telegram_handlers, "_call_core_api", mock_call_api)
        import time
        monkeypatch.setattr(time, "sleep", lambda s: None)
        cb = {"id": "cb-rwc-timeout", "data": "sys_restart_worker_confirm:gpu-01", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "не став" in text or "timeout" in text.lower() or "60s" in text


class TestBackupOperation:
    def test_backup_confirm_triggers_api(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: ({"operation_id": "b" * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "advance_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "complete_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "fail_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "audit_entry", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "_call_core_api",
                            lambda *a, **kw: {"snapshot_id": "snap-123", "status": "done"})
        cb = {"id": "cb-bkp", "data": "sys_backup_confirm", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert "створено" in adapter.answer_callback.call_args[0][1].lower() or "created" in adapter.answer_callback.call_args[0][1].lower()


class TestRestoreOperation:
    def test_restore_shows_backups_list(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "_call_core_api",
                            lambda *a, **kw: {"snapshots": [{"snapshot_id": "snap1", "created_at": "2026-01-01T00:00:00Z", "size": 100}]})
        cb = {"id": "cb-rest", "data": "sys_restore:menu", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert any("sys_restore_select" in c for c in all_callbacks)

    def test_restore_two_step_confirmation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "_call_backup_api",
                            lambda *a, **kw: {"snapshot_id": "snap1", "created_at": "2026-01-01T00:00:00Z", "size": 100})
        cb = {"id": "cb-rest-sel", "data": "sys_restore_select:snap1", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert any("sys_restore_confirm:snap1" in c for c in all_callbacks)

    def test_restore_execute_after_second_confirmation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: ({"operation_id": "c" * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "advance_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "complete_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "fail_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "audit_entry", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "_call_core_api",
                            lambda *a, **kw: {"status": "done"})
        cb = {"id": "cb-rest-exec", "data": "sys_restore_execute:snap1", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert "завершено" in adapter.answer_callback.call_args[0][1].lower() or "completed" in adapter.answer_callback.call_args[0][1].lower()

    def test_restore_execute_completes_two_phase_confirmation(self, monkeypatch):
        """Issue #137: a confirmation_required challenge is executed with its token."""
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: ({"operation_id": "d" * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "advance_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "complete_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "fail_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "audit_entry", lambda *a, **kw: None)
        calls = []

        def fake_call_api(method, path, payload=None, **kwargs):
            calls.append(path)
            if path.endswith("/restore"):
                return {"status": "confirmation_required", "confirmation_token": "tok-abc"}
            if "/restore/confirm" in path:
                return {"status": "done"}
            return {"_error": "unexpected"}

        monkeypatch.setattr(_owner_core_telegram_handlers, "_call_core_api", fake_call_api)
        cb = {"id": "cb-rest-exec2", "data": "sys_restore_execute:snap1", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert any("/restore/confirm?token=tok-abc" in c for c in calls)
        assert "завершено" in adapter.answer_callback.call_args[0][1].lower()

    def test_restore_execute_fails_when_challenge_has_no_token(self, monkeypatch):
        """Issue #137: a tokenless challenge must not be reported as success."""
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation", lambda *a, **kw: ({"operation_id": "e" * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "advance_operation", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "complete_operation", lambda *a, **kw: None)
        failed = {"called": False}
        monkeypatch.setattr(_owner_core_telegram_handlers, "fail_operation", lambda *a, **kw: failed.update(called=True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "audit_entry", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "_call_core_api",
                            lambda *a, **kw: {"status": "confirmation_required"})
        cb = {"id": "cb-rest-exec3", "data": "sys_restore_execute:snap1", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert failed["called"] is True
        assert "не надав токен" in adapter.answer_callback.call_args[0][1].lower()


class TestTestOperation:
    def test_test_menu_shows_options(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress", lambda t: None)
        cb = {"id": "cb-test", "data": "sys_test:menu", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_test_quick" in all_callbacks
        assert "sys_test_full" in all_callbacks
        assert "sys_test_node" in all_callbacks

    def test_test_quick_confirmation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        cb = {"id": "cb-test-quick", "data": "sys_test_quick", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_test_quick_confirm" in all_callbacks


class TestAccessDenial:
    """Non-admin callback for every system operation must be rejected."""

    @pytest.mark.parametrize("action", [
        "sys_status:menu", "sys_update:menu", "sys_restart:menu",
        "sys_backup:menu", "sys_restore:menu", "sys_test:menu",
    ])
    def test_non_admin_callback_denied(self, monkeypatch, action):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: False)
        cb = {"id": "cb-denied", "data": action,
              "message": {"chat": {"id": "99"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "Доступ заборонено" in text


class TestIdempotency:
    """Duplicate callbacks must not create duplicate operations."""

    def test_restart_idempotency(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress",
                            lambda t: {"operation_id": "r" * 32, "type": "restart"})
        cb = {"id": "cb-restart-id", "data": "sys_restart:menu",
              "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "вже в процесі" in text or "процесі" in text

    def test_backup_idempotency(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation",
                            lambda *a, **kw: ({"operation_id": "b" * 32, "type": "backup"}, False))
        cb = {"id": "cb-backup-id", "data": "sys_backup_confirm",
              "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "вже в процесі" in text or "процесі" in text

    def test_restore_idempotency(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_operation_in_progress",
                            lambda t: {"operation_id": "e" * 32, "type": "restore"})
        cb = {"id": "cb-restore-id", "data": "sys_restore_select:snap1",
              "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "процесі" in text

    def test_test_idempotency(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation",
                            lambda *a, **kw: ({"operation_id": "t" * 32, "type": "test"}, False))
        cb = {"id": "cb-test-id", "data": "sys_test_quick_confirm",
              "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "вже в процесі" in text or "процесі" in text

    def test_duplicate_callback_dedup(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        _owner_core_telegram_handlers._telegram_system_callbacks.clear()

    def test_duplicate_callback_dedup_survives_process_state_reset(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        _owner_core_telegram_handlers._telegram_system_callbacks.clear()
        cb = {"id": "cb-durable-dedup", "data": "sys_cancel",
              "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        # Recreate the facade as a process restart would; the on-disk claim remains.
        from core.telegram_callbacks import DurableCallbackSet
        _owner_core_telegram_handlers._telegram_system_callbacks = DurableCallbackSet()
        adapter.reset_mock()
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert "вже оброблено" in adapter.answer_callback.call_args[0][1]
        _owner_core_telegram_handlers._telegram_system_callbacks.clear()
        cb = {"id": "cb-dedup", "data": "sys_cancel",
              "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        first_call = adapter.answer_callback.called
        adapter.reset_mock()
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        second_call_text = adapter.answer_callback.call_args[0][1] if adapter.answer_callback.called else ""
        assert first_call
        assert "вже оброблено" in second_call_text
        _owner_core_telegram_handlers._telegram_system_callbacks.clear()


class TestFailurePaths:
    def test_restart_core_backend_failure(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_operation_in_progress', lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'get_or_create_operation', lambda *a, **kw: ({'operation_id': 'f' * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, 'begin_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'fail_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'audit_entry', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, '_call_core_api', lambda *a, **kw: {'_error': 'HTTP 500: Internal error'})
        cb = {'id': 'cb-fr', 'data': 'sys_restart_core_confirm', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert chr(10060) in adapter.answer_callback.call_args[0][1]

    def test_backup_backend_failure(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_operation_in_progress', lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'get_or_create_operation', lambda *a, **kw: ({'operation_id': 'f' * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, 'begin_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'advance_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'fail_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'audit_entry', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, '_call_core_api', lambda *a, **kw: {'_error': 'Connection refused'})
        cb = {'id': 'cb-fb', 'data': 'sys_backup_confirm', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert chr(10060) in adapter.answer_callback.call_args[0][1]

    def test_restore_backend_failure(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_operation_in_progress', lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'get_or_create_operation', lambda *a, **kw: ({'operation_id': 'f' * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, 'begin_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'advance_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'fail_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'audit_entry', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, '_call_core_api', lambda *a, **kw: {'_error': 'Timeout'})
        cb = {'id': 'cb-fres', 'data': 'sys_restore_execute:snap1', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert chr(10060) in adapter.answer_callback.call_args[0][1]

    def test_test_quick_backend_failure(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_operation_in_progress', lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'get_or_create_operation', lambda *a, **kw: ({'operation_id': 'f' * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, 'begin_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'fail_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'audit_entry', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, '_call_core_api', lambda *a, **kw: {'_error': 'HTTP 503'})
        cb = {'id': 'cb-ft', 'data': 'sys_test_quick_confirm', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert chr(10060) in adapter.answer_callback.call_args[0][1]


class TestRestartWorkerFlow:
    def test_restart_worker_shows_nodes(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_operation_in_progress', lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, '_sync_internal_api', lambda *a, **kw: [{'node_id': 'gpu-01', 'role': 'gpu'}])
        cb = {'id': 'cb-rw', 'data': 'sys_restart_worker', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.send_message.called
        kb = adapter.send_message.call_args[1]['reply_markup']['inline_keyboard']
        all_cb = [b['callback_data'] for row in kb for b in row]
        assert any('sys_restart_worker_select' in c for c in all_cb)

    def test_restart_worker_execute(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_operation_in_progress', lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'get_or_create_operation', lambda *a, **kw: ({'operation_id': 'w' * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, 'begin_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'advance_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'complete_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'fail_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'audit_entry', lambda *a, **kw: None)
        call_count = {"count": 0}
        def mock_call_api(method, path, payload=None, timeout=30):
            call_count["count"] += 1
            if path == "/api/nodes/gpu-01/actions":
                return {"status": "restart_requested", "restart_operation_id": "restart-2"}
            if path == "/api/nodes/gpu-01":
                return ({"status": "READY", "update_state": {
                    "restart_ack": {"operation_id": "restart-2"}}}
                        if call_count["count"] > 1 else {"status": "UPDATING"})
        monkeypatch.setattr(_owner_core_telegram_handlers, '_call_core_api', mock_call_api)
        import time
        monkeypatch.setattr(time, 'sleep', lambda s: None)
        cb = {'id': 'cb-rwc', 'data': 'sys_restart_worker_confirm:gpu-01', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert chr(9989) in adapter.answer_callback.call_args[0][1]


class TestTestFullFlow:
    def test_test_full_confirmation(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        cb = {'id': 'cb-tf', 'data': 'sys_test_full', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.send_message.called
        kb = adapter.send_message.call_args[1]['reply_markup']['inline_keyboard']
        all_cb = [b['callback_data'] for row in kb for b in row]
        assert 'sys_test_full_confirm' in all_cb

    def test_test_full_execute(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_operation_in_progress', lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'get_or_create_operation', lambda *a, **kw: ({'operation_id': 't' * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, 'begin_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'complete_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'fail_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'audit_entry', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, '_call_core_api', lambda *a, **kw: {'scope': 'full', 'result': 'OK'})
        cb = {'id': 'cb-tfc', 'data': 'sys_test_full_confirm', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert chr(9989) in adapter.answer_callback.call_args[0][1]


class TestTestNodeFlow:
    def test_test_node_shows_nodes(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_operation_in_progress', lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, '_sync_internal_api', lambda *a, **kw: [{'node_id': 'gpu-01', 'role': 'gpu'}])
        cb = {'id': 'cb-tn', 'data': 'sys_test_node', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.send_message.called
        kb = adapter.send_message.call_args[1]['reply_markup']['inline_keyboard']
        all_cb = [b['callback_data'] for row in kb for b in row]
        assert any('sys_test_node_select' in c for c in all_cb)

    def test_test_node_execute(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_operation_in_progress', lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'get_or_create_operation', lambda *a, **kw: ({'operation_id': 'n' * 32}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, 'begin_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'complete_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'fail_operation', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'audit_entry', lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, '_call_core_api', lambda *a, **kw: {'status': 'ok'})
        cb = {'id': 'cb-tnc', 'data': 'sys_test_node_confirm:gpu-01', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert chr(9989) in adapter.answer_callback.call_args[0][1]


class TestRestoreNoBackups:
    def test_restore_shows_no_backups(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_operation_in_progress', lambda t: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, '_call_backup_api', lambda *a, **kw: None)
        cb = {'id': 'cb-rnb', 'data': 'sys_restore:menu', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert 'Backup Node' in text


class TestCancelOperation:
    def test_sys_cancel(self, monkeypatch):
        module = importlib.import_module('core.app')
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, 'TelegramAdapter', lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, 'is_admin_chat', lambda chat_id: True)
        cb = {'id': 'cb-cancel', 'data': 'sys_cancel', 'message': {'chat': {'id': '42'}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert 'скасовано' in adapter.answer_callback.call_args[0][1].lower()


class TestCallbackRouting:
    def test_unknown_action_returns_error(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        cb = {"id": "cb-unk", "data": "unknown_action", "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called


class TestDurableCallbackCrashRecovery:
    """Issue #174: a claim left CLAIMED by a crash must be resumable on replay."""

    def test_claim_allows_resume_after_crash_before_effect(self):
        from core import telegram_callbacks as tc
        tc.claim_callback("cb-crash-1", actor="telegram:42", action="sys_cancel")
        # Crash after persisting the claim, before the side effect: replays must
        # be able to run the effect instead of being told it is already done.
        record, reclaimed = tc.claim_callback(
            "cb-crash-1", actor="telegram:42", action="sys_cancel")
        assert reclaimed is True
        assert record["status"] == "CLAIMED"
        # Completing the effect is terminal: a later replay must not re-run it.
        tc.complete_callback("cb-crash-1")
        done, reclaimed_after = tc.claim_callback(
            "cb-crash-1", actor="telegram:42", action="sys_cancel")
        assert reclaimed_after is False
        assert done["status"] == "COMPLETED"

    def test_failed_callback_is_terminal_and_not_replayed(self):
        from core import telegram_callbacks as tc
        tc.claim_callback("cb-fail-term", actor="telegram:42", action="sys_cancel")
        tc.complete_callback("cb-fail-term", status="FAILED")
        record, claimed = tc.claim_callback(
            "cb-fail-term", actor="telegram:42", action="sys_cancel")
        assert claimed is False
        assert record["status"] == "FAILED"

    def test_handler_resumes_claimed_callback_after_process_restart(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        _owner_core_telegram_handlers._telegram_system_callbacks.clear()
        from core.telegram_callbacks import claim_callback, DurableCallbackSet
        # Crash after the claim and before the effect: the record stays CLAIMED.
        claim_callback("cb-crash-handler", actor="telegram:42", action="sys_cancel")
        _owner_core_telegram_handlers._telegram_system_callbacks = DurableCallbackSet()  # fresh process
        cb = {"id": "cb-crash-handler", "data": "sys_cancel",
              "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        # The effect actually ran (the callback was not treated as done).
        assert "скасовано" in adapter.answer_callback.call_args[0][1].lower()
        _owner_core_telegram_handlers._telegram_system_callbacks.clear()

    def test_update_confirm_exception_fails_the_created_operation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        failed: dict = {}
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(_owner_core_telegram_handlers, "get_or_create_operation",
                            lambda *a, **kw: ({"operation_id": "u" * 32, "type": "update"}, True))
        monkeypatch.setattr(_owner_core_telegram_handlers, "audit_entry", lambda *a, **kw: None)
        monkeypatch.setattr(_owner_core_telegram_handlers, "begin_operation", lambda *a, **kw: None)

        def boom(*_args, **_kwargs):
            raise RuntimeError("host agent unreachable")

        monkeypatch.setattr(_owner_core_telegram_handlers, "request_update", boom)
        monkeypatch.setattr(_owner_core_telegram_handlers, "fail_operation",
                            lambda op_id, msg: failed.update(op_id=op_id, msg=msg))
        cb = {"id": "cb-upd-conf-fail", "data": "sys_update_confirm",
              "message": {"chat": {"id": "42"}}}
        _owner_core_telegram_handlers._handle_telegram_callback(cb)
        # A failed confirm must not leave the created operation RUNNING forever.
        assert failed.get("op_id") == "u" * 32
        assert "host agent unreachable" in failed.get("msg", "")
        assert adapter.answer_callback.called
        assert "не запущено" in adapter.answer_callback.call_args[0][1]


class TestDurableCallbackOperationLinking:
    """Issue #230: durable actor/target confirmation via operation linking.

    A CLAIMED callback whose associated operation reached a terminal state must
    not be replayed (effect→crash→persist gap).  link_operation binds the
    callback record to its operation so claim_callback can detect terminal
    status on replay.
    """

    def test_link_operation_binds_callback_to_operation(self):
        from core import telegram_callbacks as tc
        from core.operations import create_operation
        op = create_operation("update", "telegram:42", target="apply")
        tc.claim_callback("cb-linked", actor="telegram:42", action="sys_update_confirm",
                          target="apply", operation_id=op["operation_id"])
        record = tc._read()["cb-linked"]
        assert record["operation_id"] == op["operation_id"]

    def test_claim_does_not_replay_when_operation_completed(self):
        from core import telegram_callbacks as tc
        from core.operations import create_operation, complete_operation
        op = create_operation("update", "telegram:42", target="apply")
        complete_operation(op["operation_id"], {"status": "healthy"})
        tc.claim_callback("cb-completed-op", actor="telegram:42", action="sys_update_confirm",
                          target="apply", operation_id=op["operation_id"])
        record, claimed = tc.claim_callback(
            "cb-completed-op", actor="telegram:42", action="sys_update_confirm",
            target="apply")
        assert claimed is False
        assert record["status"] == "CLAIMED"

    def test_claim_does_not_replay_when_operation_failed(self):
        from core import telegram_callbacks as tc
        from core.operations import create_operation, fail_operation
        op = create_operation("update", "telegram:42", target="apply")
        fail_operation(op["operation_id"], "host agent unreachable")
        tc.claim_callback("cb-failed-op", actor="telegram:42", action="sys_update_confirm",
                          target="apply", operation_id=op["operation_id"])
        record, claimed = tc.claim_callback(
            "cb-failed-op", actor="telegram:42", action="sys_update_confirm",
            target="apply")
        assert claimed is False
        assert record["status"] == "CLAIMED"

    def test_claim_replays_when_operation_is_still_active(self):
        from core import telegram_callbacks as tc
        from core.operations import create_operation, begin_operation
        op = create_operation("update", "telegram:42", target="apply")
        begin_operation(op["operation_id"], "Запит на оновлення прийнято")
        tc.claim_callback("cb-active-op", actor="telegram:42", action="sys_update_confirm",
                          target="apply", operation_id=op["operation_id"])
        record, claimed = tc.claim_callback(
            "cb-active-op", actor="telegram:42", action="sys_update_confirm",
            target="apply")
        assert claimed is True
        assert record["status"] == "CLAIMED"

    def test_claim_replays_when_no_operation_linked(self):
        from core import telegram_callbacks as tc
        # CLAIMED without operation_id: crash before effect, replay must run.
        tc.claim_callback("cb-no-op", actor="telegram:42", action="sys_cancel")
        record, claimed = tc.claim_callback(
            "cb-no-op", actor="telegram:42", action="sys_cancel")
        assert claimed is True
        assert record["status"] == "CLAIMED"

    def test_link_operation_then_claim_detects_terminal(self, monkeypatch):
        """End-to-end: claim → effect completes operation → link → replay blocked."""
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(_owner_core_telegram_handlers, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(_owner_core_telegram_handlers, "is_admin_chat", lambda chat_id: True)

        from core import telegram_callbacks as tc
        from core.operations import create_operation, complete_operation

        # First run: claim the callback, effect runs, operation completes.
        tc.claim_callback("cb-e2e", actor="telegram:42", action="sys_update_confirm",
                          target="apply")
        op = create_operation("update", "telegram:42", target="apply")
        complete_operation(op["operation_id"], {"status": "healthy"})
        tc.link_operation("cb-e2e", op["operation_id"])

        # Replay: claim_callback must detect the terminal operation and not replay.
        record, claimed = tc.claim_callback(
            "cb-e2e", actor="telegram:42", action="sys_update_confirm", target="apply")
        assert claimed is False
        assert record["status"] == "CLAIMED"
