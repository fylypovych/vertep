"""Tests for Telegram system operations: Status, Update, Restart, Backup, Restore, Test."""
import importlib
from unittest.mock import Mock, patch


class TestSystemMenu:
    def test_system_menu_admin(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module.store, "jobs", {})
        msg = {"text": "/system", "chat": {"id": "42"}, "message_id": 1}
        result = module._handle_telegram_message("42", "1", "/system", msg)
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
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: False)
        msg = {"text": "/system", "chat": {"id": "99"}, "message_id": 1}
        module._handle_telegram_message("99", "1", "/system", msg)
        assert "Доступ заборонено" in adapter.send_message.call_args[0][1]


class TestStatusOperation:
    def test_status_callback_returns_text(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module, "list_operations", lambda n: [])
        monkeypatch.setattr(module, "update_status", lambda: {"current_version": "0.0.1.1"})
        monkeypatch.setattr(module.store, "jobs", {})
        monkeypatch.setattr(module, "get_system_state", lambda: {"state": "NORMAL"})
        monkeypatch.setattr(module, "_sync_internal_api", lambda *a, **kw: None)
        cb = {"id": "cb-status", "data": "sys_status:menu", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        text = adapter.answer_callback.call_args[0][1]
        assert "Система:" in text or "Нормальний" in text or "NORMAL" in text or "IDLE" in text


class TestUpdateOperation:
    def test_update_menu_shows_confirmation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(module, "update_status", lambda: {"current_version": "0.0.1.1"})
        cb = {"id": "cb-upd-1", "data": "sys_update:menu", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_update_check" in all_callbacks

    def test_update_idempotency_blocks_duplicate(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module, "is_operation_in_progress",
                            lambda t: {"operation_id": "abc12345"} if t == "update" else None)
        cb = {"id": "cb-upd-2", "data": "sys_update:menu", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        text = adapter.answer_callback.call_args[0][1]
        assert "вже в процесі" in text


class TestRestartOperation:
    def test_restart_menu_shows_core_worker(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module, "is_operation_in_progress", lambda t: None)
        cb = {"id": "cb-restart", "data": "sys_restart:menu", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_restart_core" in all_callbacks
        assert "sys_restart_worker" in all_callbacks

    def test_restart_core_confirmation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        cb = {"id": "cb-rcore", "data": "sys_restart_core", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_restart_core_confirm" in all_callbacks

    def test_restart_core_execute(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(module, "create_operation", lambda *a, **kw: {"operation_id": "a" * 32})
        monkeypatch.setattr(module, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "complete_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "fail_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "_call_core_api", lambda *a, **kw: {"status": "restart_requested"})
        cb = {"id": "cb-rcore-run", "data": "sys_restart_core_confirm", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert "ініиційовано" in adapter.answer_callback.call_args[0][1].lower() or "requested" in adapter.answer_callback.call_args[0][1].lower()


class TestBackupOperation:
    def test_backup_confirm_triggers_api(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(module, "create_operation", lambda *a, **kw: {"operation_id": "b" * 32})
        monkeypatch.setattr(module, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "advance_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "complete_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "fail_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "audit_entry", lambda *a, **kw: None)
        monkeypatch.setattr(module, "_call_core_api",
                            lambda *a, **kw: {"snapshot_id": "snap-123", "status": "done"})
        cb = {"id": "cb-bkp", "data": "sys_backup_confirm", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert "створено" in adapter.answer_callback.call_args[0][1].lower() or "created" in adapter.answer_callback.call_args[0][1].lower()


class TestRestoreOperation:
    def test_restore_shows_backups_list(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module, "_call_backup_api",
                            lambda *a, **kw: {"snapshots": [{"snapshot_id": "snap1", "created_at": "2026-01-01T00:00:00Z", "size": 100}]})
        cb = {"id": "cb-rest", "data": "sys_restore:menu", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert any("sys_restore_select" in c for c in all_callbacks)

    def test_restore_two_step_confirmation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module, "_call_backup_api",
                            lambda *a, **kw: {"snapshot_id": "snap1", "created_at": "2026-01-01T00:00:00Z", "size": 100})
        cb = {"id": "cb-rest-sel", "data": "sys_restore_select:snap1", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert any("sys_restore_confirm:snap1" in c for c in all_callbacks)

    def test_restore_execute_after_second_confirmation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module, "is_operation_in_progress", lambda t: None)
        monkeypatch.setattr(module, "create_operation", lambda *a, **kw: {"operation_id": "c" * 32})
        monkeypatch.setattr(module, "begin_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "advance_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "complete_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "fail_operation", lambda *a, **kw: None)
        monkeypatch.setattr(module, "audit_entry", lambda *a, **kw: None)
        monkeypatch.setattr(module, "_call_backup_api",
                            lambda *a, **kw: {"status": "done"})
        cb = {"id": "cb-rest-exec", "data": "sys_restore_execute:snap1", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
        assert "завершено" in adapter.answer_callback.call_args[0][1].lower() or "completed" in adapter.answer_callback.call_args[0][1].lower()


class TestTestOperation:
    def test_test_menu_shows_options(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        monkeypatch.setattr(module, "is_operation_in_progress", lambda t: None)
        cb = {"id": "cb-test", "data": "sys_test:menu", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_test_quick" in all_callbacks
        assert "sys_test_full" in all_callbacks
        assert "sys_test_node" in all_callbacks

    def test_test_quick_confirmation(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        cb = {"id": "cb-test-quick", "data": "sys_test_quick", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.send_message.called
        keyboard = adapter.send_message.call_args[1]["reply_markup"]["inline_keyboard"]
        all_callbacks = [b["callback_data"] for row in keyboard for b in row]
        assert "sys_test_quick_confirm" in all_callbacks


class TestCallbackRouting:
    def test_unknown_action_returns_error(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module, "is_admin_chat", lambda chat_id: True)
        cb = {"id": "cb-unk", "data": "unknown_action", "message": {"chat": {"id": "42"}}}
        module._handle_telegram_callback(cb)
        assert adapter.answer_callback.called
