"""Telegram system menu: ``sys_*`` callbacks and their dispatch table."""

from __future__ import annotations

import re
from typing import Callable

from adapters.telegram import TelegramAdapter
from core.api._internal import _sync_internal_api
from core.operations import (
    advance_operation,
    audit_entry,
    begin_operation,
    complete_operation,
    fail_operation,
    get_or_create_operation,
    is_operation_in_progress,
    link_operation_request,
)
from core.telegram_callbacks import set_callback_target
from core.update_manager import (
    request_update,
    update_status,
)
from core.version import application_version

from .common import (
    _record_operation_field,
    _queue_operation_notification,
    _wait_for_core_healthy,
    _wait_for_worker_ready,
    _call_core_api,
)
from .status import (
    _system_status_text,
    _recover_stale_update_operations,
    _reconcile_restart_operations,
    _reconcile_update_operations,
    _release_notes_for,
    _update_target_version,
    _send_confirmation,
    _send_backups_list,
    _send_nodes_list,
)


# ---------------------------------------------------------------------------
# System callback dispatch table.
# ---------------------------------------------------------------------------

def _sys_status(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    return TelegramAdapter().answer_callback(callback_id, _system_status_text())


def _sys_update(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    _recover_stale_update_operations()
    _reconcile_update_operations()
    existing = is_operation_in_progress("update")
    if existing:
        return TelegramAdapter().answer_callback(
            callback_id, f"Оновлення вже в процесі (#{existing['operation_id'][:8]})."
        )
    update_info = {}
    try:
        update_info = update_status() or {}
    except Exception:
        pass
    current = update_info.get("current_version") or application_version()
    available = update_info.get("available_version")
    message = f"🔄 Поточна версія: {current}\n"
    if available:
        message += f"Доступна: {available}\n"
        notes = _release_notes_for(available)
        if notes:
            message += "\nЗміни:\n" + "\n".join(notes) + "\n"
        message += "\nОновити Vertep?"
        confirm_action = "sys_update_confirm"
    else:
        message += "Доступних оновлень немає.\n\nПеревірети доступність оновлення?"
        confirm_action = "sys_update_check"
    set_callback_target(callback_id, None)
    return _send_confirmation(chat_id, message, confirm_action, "sys_cancel")


def _sys_update_check(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    try:
        _recover_stale_update_operations()
        op, created = get_or_create_operation("update", f"telegram:{chat_id}", target="check")
        if not created:
            return TelegramAdapter().answer_callback(
                callback_id, f"Перевірка оновлення вже в процесі (#{op['operation_id'][:8]})."
            )
        audit_entry(op["operation_id"], "INTERNAL_CALL", "request_update check", f"telegram:{chat_id}")
        request = request_update("check")
        if request:
            link_operation_request(op["operation_id"], request)
        begin_operation(op["operation_id"], "Перевірка доступного оновлення")
        return TelegramAdapter().answer_callback(
            callback_id,
            f"Перевірка оновлення запущена. ID операції: {op['operation_id'][:8]}"
        )
    except Exception as error:
        return TelegramAdapter().answer_callback(callback_id, f"❌ Перевірка не запущена: {error}")


def _sys_update_confirm(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    op = None
    try:
        _recover_stale_update_operations()
        _reconcile_update_operations()
        target = _update_target_version()
        set_callback_target(callback_id, target)
        op, created = get_or_create_operation("update", f"telegram:{chat_id}", target="apply")
        if not created:
            return TelegramAdapter().answer_callback(
                callback_id, f"Оновлення вже в процесі (#{op['operation_id'][:8]})."
            )
        audit_entry(op["operation_id"], "INTERNAL_CALL", "request_update update", f"telegram:{chat_id}")
        request = request_update("update")
        if request:
            link_operation_request(op["operation_id"], request)
        begin_operation(op["operation_id"], "Запит на оновлення прийнято")
        audit_entry(op["operation_id"], "RUNNING", "Update started via Telegram", f"telegram:{chat_id}")
        confirmed = f" → {target}" if target else ""
        return TelegramAdapter().answer_callback(
            callback_id,
            f"✅ Підтверджено: telegram:{chat_id}{confirmed}. "
            f"ID операції: {op['operation_id'][:8]}"
        )
    except Exception as error:
        if op is not None:
            try:
                fail_operation(op["operation_id"], str(error))
            except Exception:
                pass
        return TelegramAdapter().answer_callback(callback_id, f"❌ Оновлення не запущено: {error}")


def _sys_update_cancel(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    return TelegramAdapter().answer_callback(callback_id, "Оновлення скасовано.")


def _sys_restart(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    existing = is_operation_in_progress("restart")
    if existing:
        return TelegramAdapter().answer_callback(
            callback_id, f"Перезапуск вже в процесі (#{existing['operation_id'][:8]})."
        )
    keyboard = {"inline_keyboard": [
        [{"text": "🔁 Core", "callback_data": "sys_restart_core"},
         {"text": "🔁 Worker", "callback_data": "sys_restart_worker"}],
        [{"text": "❌ Скасувати", "callback_data": "sys_cancel"}],
    ]}
    return TelegramAdapter().send_message(
        chat_id, "Оберіть що перезапустити:", reply_markup=keyboard
    )


def _sys_restart_core(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    return _send_confirmation(
        chat_id, "🔄 Перезапустити CORE сервіси?\nВсі активні задачі будуть продовжені.",
        "sys_restart_core_confirm", "sys_cancel"
    )


def _sys_restart_core_confirm(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    _reconcile_restart_operations()
    op, created = get_or_create_operation("restart", f"telegram:{chat_id}", target="core")
    if not created:
        return TelegramAdapter().answer_callback(
            callback_id, f"Перезапуск вже в процесі (#{op['operation_id'][:8]})."
        )
    begin_operation(op["operation_id"], "Перезапуск CORE")
    audit_entry(op["operation_id"], "INTERNAL_CALL", "POST /api/system/restart target=core", f"telegram:{chat_id}")
    try:
        result = _call_core_api("POST", "/api/system/restart", {"target": "core"}, timeout=15)
        if result and "_error" in result:
            fail_operation(op["operation_id"], result["_error"])
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
            _queue_operation_notification(
                op, "❌", f"Перезапуск CORE не вдався: {result['_error'][:200]}")
            return TelegramAdapter().answer_callback(
                callback_id, f"❌ Перезапуск CORE не вдався: {result['_error'][:200]}"
            )
        previous_instance_id = (result or {}).get("previous_runtime_instance_id")
        if not previous_instance_id:
            fail_operation(op["operation_id"], "CORE restart response did not include process identity")
            _queue_operation_notification(
                op, "❌",
                "Перезапуск CORE не підтверджено: API не повернув ідентифікатор процесу.")
            return TelegramAdapter().answer_callback(
                callback_id, "❌ Перезапуск CORE не підтверджено: API не повернув ідентифікатор процесу."
            )
        _record_operation_field(op["operation_id"],
                                 previous_runtime_instance_id=previous_instance_id)
        advance_operation(op["operation_id"], "Очікування здоров'я CORE", 50, "Перезапуск ініційовано, очікуємо HEALTHY")
        healthy = _wait_for_core_healthy(previous_instance_id)
        if not healthy:
            fail_operation(op["operation_id"], "CORE не став HEALTHY після перезапуску (timeout 60s)")
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", "GET /api/health timeout", f"telegram:{chat_id}")
            _queue_operation_notification(
                op, "❌",
                f"Перезапуск CORE: сервіс не став HEALTHY за 60s. ID: {op['operation_id'][:8]}")
            return TelegramAdapter().answer_callback(
                callback_id, f"❌ Перезапуск CORE: сервіс не став HEALTHY за 60s. ID: {op['operation_id'][:8]}"
            )
        complete_operation(op["operation_id"], {"status": "healthy", "message": "CORE перезапущено успішно"})
        audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", "CORE healthy", f"telegram:{chat_id}")
        _queue_operation_notification(
            op, "✅", f"CORE перезапущено і HEALTHY. ID: {op['operation_id'][:8]}")
        return TelegramAdapter().answer_callback(
            callback_id, f"✅ CORE перезапущено і HEALTHY. ID: {op['operation_id'][:8]}"
        )
    except Exception as error:
        fail_operation(op["operation_id"], str(error))
        _queue_operation_notification(op, "❌", f"Перезапуск CORE не вдався: {error}")
        return TelegramAdapter().answer_callback(callback_id, f"❌ Перезапуск CORE не вдався: {error}")


def _sys_restart_worker(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    nodes = _sync_internal_api("GET", "CORE_API_URL", "/api/nodes") or []
    if not nodes or not isinstance(nodes, list):
        return TelegramAdapter().answer_callback(
            callback_id, "Немає зареєстрованих вузлів."
        )
    return _send_nodes_list(chat_id, nodes, "sys_restart_worker_select")


def _sys_restart_worker_select(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    node_id = payload
    if not node_id or not re.fullmatch(r"[a-z0-9-]+", node_id):
        return TelegramAdapter().answer_callback(callback_id, "Некоректний вузол.")
    return _send_confirmation(
        chat_id, f"🔄 Перезапустити Worker {node_id}?\nАктивні задачі будуть передані іншим вузлам.",
        f"sys_restart_worker_confirm:{node_id}", "sys_cancel"
    )


def _sys_restart_worker_confirm(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    node_id = payload
    if not node_id:
        return TelegramAdapter().answer_callback(callback_id, "Невірний вузол.")
    _reconcile_restart_operations()
    op, created = get_or_create_operation("restart", f"telegram:{chat_id}", target=node_id)
    if not created:
        return TelegramAdapter().answer_callback(
            callback_id, f"Перезапуск вже в процесі (#{op['operation_id'][:8]})."
        )
    begin_operation(op["operation_id"], f"Перезапуск вузла {node_id}")
    advance_operation(op["operation_id"], "Drain", 20,
                      f"Вузол {node_id} виходить із розподілу задач")
    audit_entry(op["operation_id"], "INTERNAL_CALL", f"POST /api/nodes/{node_id}/actions action=restart", f"telegram:{chat_id}")
    try:
        result = _call_core_api("POST", f"/api/nodes/{node_id}/actions",
                                {"action": "restart", "reason": f"telegram restart {op['operation_id'][:8]}"},
                                timeout=15)
        if result and "_error" in result:
            fail_operation(op["operation_id"], result["_error"])
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
            _queue_operation_notification(
                op, "❌", f"Перезапуск {node_id} не вдався: {result['_error'][:200]}")
            return TelegramAdapter().answer_callback(
                callback_id, f"❌ Перезапуск {node_id} не вдався: {result['_error'][:200]}"
            )
        restart_operation_id = (result or {}).get("restart_operation_id")
        if not restart_operation_id:
            fail_operation(op["operation_id"], f"Worker {node_id} restart response has no operation id")
            _queue_operation_notification(
                op, "❌", f"Перезапуск {node_id} не підтверджено: відсутній ID операції.")
            return TelegramAdapter().answer_callback(
                callback_id, f"❌ Перезапуск {node_id} не підтверджено: відсутній ID операції."
            )
        _record_operation_field(op["operation_id"],
                                restart_operation_id=restart_operation_id)
        advance_operation(op["operation_id"], "Drain", 50,
                          "Перезапуск ініційовано, очікуємо підтвердження від вузла")
        ready = _wait_for_worker_ready(node_id, restart_operation_id)
        if not ready:
            fail_operation(op["operation_id"], f"Worker {node_id} не став READY після перезапуску (timeout 60s)")
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"GET /api/nodes/{node_id} timeout", f"telegram:{chat_id}")
            _queue_operation_notification(
                op, "❌",
                f"Перезапуск {node_id}: вузол не став READY за 60s. ID: {op['operation_id'][:8]}")
            return TelegramAdapter().answer_callback(
                callback_id, f"❌ Перезапуск {node_id}: вузол не став READY за 60s. ID: {op['operation_id'][:8]}"
            )
        complete_operation(op["operation_id"], {"status": "ready", "node_id": node_id,
                                                "restart_operation_id": restart_operation_id,
                                                "runtime_instance_id": (ready.get("update_state") or {}).get("restart_ack", {}).get("runtime_instance_id"),
                                                "message": f"Worker {node_id} перезапущено успішно"})
        audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"Worker {node_id} READY", f"telegram:{chat_id}")
        _queue_operation_notification(
            op, "✅", f"Вузол {node_id} перезапущено і READY. ID: {op['operation_id'][:8]}")
        return TelegramAdapter().answer_callback(
            callback_id, f"✅ Вузол {node_id} перезапущено і READY. ID: {op['operation_id'][:8]}"
        )
    except Exception as error:
        fail_operation(op["operation_id"], str(error))
        audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"exception: {error}", f"telegram:{chat_id}")
        _queue_operation_notification(op, "❌", f"Перезапуск {node_id} не вдався: {error}")
        return TelegramAdapter().answer_callback(callback_id, f"❌ Перезапуск {node_id} не вдався: {error}")


def _sys_backup(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    return _send_confirmation(
        chat_id, "💾 Створити резервну копію зараз?",
        "sys_backup_confirm", "sys_cancel"
    )


def _sys_backup_confirm(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    op, created = get_or_create_operation("backup", f"telegram:{chat_id}")
    if not created:
        return TelegramAdapter().answer_callback(
            callback_id, f"Резервна копія вже в процесі (#{op['operation_id'][:8]})."
        )
    begin_operation(op["operation_id"], "Створення резервної копії")
    advance_operation(op["operation_id"], "snapshot", 10, "Snapshot заплановано")
    audit_entry(op["operation_id"], "INTERNAL_CALL", "POST /api/system/backups", f"telegram:{chat_id}")
    audit_entry(op["operation_id"], "snapshot", "Backup started via Telegram", f"telegram:{chat_id}")
    try:
        result = _call_core_api("POST", "/api/system/backups", {}, timeout=120)
        if result and "_error" in result:
            fail_operation(op["operation_id"], result["_error"])
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"❌ Backup не вдався: {result['_error'][:200]}"
            )
        if result and result.get("snapshot_id"):
            complete_operation(op["operation_id"], result)
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"snapshot_id={result['snapshot_id']}", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id,
                f"✅ Backup створено. Snapshot: {result['snapshot_id'][:18]} ID операції: {op['operation_id'][:8]}"
            )
        advance_operation(op["operation_id"], "snapshot", 20, "Snapshot створюється")
        audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", "backup scheduled", f"telegram:{chat_id}")
        return TelegramAdapter().answer_callback(
            callback_id, f"✅ Backup заплановано. ID операції: {op['operation_id'][:8]}"
        )
    except Exception as error:
        fail_operation(op["operation_id"], str(error))
        audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"exception: {error}", f"telegram:{chat_id}")
        return TelegramAdapter().answer_callback(callback_id, f"❌ Backup не вдався: {error}")


def _sys_backup_cancel(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    return TelegramAdapter().answer_callback(callback_id, "Операцію скасовано.")


def _sys_restore(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    backups = _call_core_api("GET", "/api/system/backups", timeout=15)
    if backups is None:
        return TelegramAdapter().answer_callback(
            callback_id, "Backup Node недоступний. Спочатку налаштуйте Backup Node."
        )
    snapshots = (backups or {}).get("snapshots", []) if isinstance(backups, dict) else []
    if not snapshots:
        return TelegramAdapter().answer_callback(callback_id, "Немає доступних резервних копій.")
    return _send_backups_list(chat_id, snapshots)


def _sys_restore_select(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    snapshot_id = payload
    if not snapshot_id or not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        return TelegramAdapter().answer_callback(callback_id, "Некоректний ID snapshot.")
    existing = is_operation_in_progress("restore")
    if existing:
        return TelegramAdapter().answer_callback(
            callback_id, f"Відновлення вже в процесі (#{existing['operation_id'][:8]})."
        )
    backup_detail = _call_core_api("GET", f"/api/system/backups/{snapshot_id}", timeout=15) or {}
    audit_entry(snapshot_id, "INTERNAL_CALL", f"GET /api/system/backups/{snapshot_id}", f"telegram:{chat_id}")
    snapshot = backup_detail if "snapshot_id" in backup_detail else {"snapshot_id": snapshot_id}
    message = (f"⚠️ ПЕРШЕ підтвердження відновлення\n"
               f"Snapshot: {snapshot.get('snapshot_id', snapshot_id)}\n"
               f"Створено: {snapshot.get('created_at', '?')}\n"
               f"Розмір: {snapshot.get('size', '?')} байт\n\n"
               f"Це замінить поточні дані. Продовжити?")
    return _send_confirmation(
        chat_id, message, f"sys_restore_confirm:{snapshot_id}", "sys_cancel"
    )


def _sys_restore_confirm(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    snapshot_id = payload
    if not snapshot_id or not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        return TelegramAdapter().answer_callback(callback_id, "Некоректний ID snapshot.")
    message = (f"⚠️ ДРУГЕ явне підтвердження\n"
               f"Ви впевнені, що хочете відновити snapshot {snapshot_id}? "
               f"Ця дія незворотна.")
    return _send_confirmation(
        chat_id, message, f"sys_restore_execute:{snapshot_id}", "sys_cancel"
    )


def _sys_restore_execute(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    snapshot_id = payload
    if not snapshot_id or not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        return TelegramAdapter().answer_callback(callback_id, "Некоректний ID snapshot.")
    op, created = get_or_create_operation("restore", f"telegram:{chat_id}", target=snapshot_id)
    if not created:
        return TelegramAdapter().answer_callback(
            callback_id, f"Відновлення вже в процесі (#{op['operation_id'][:8]})."
        )
    begin_operation(op["operation_id"], "Відновлення")
    advance_operation(op["operation_id"], "decrypt", 10, f"Відновлення snapshot {snapshot_id[:18]}")
    audit_entry(op["operation_id"], "INTERNAL_CALL", f"POST /api/system/backups/{snapshot_id}/restore", f"telegram:{chat_id}")
    audit_entry(op["operation_id"], "restore", f"Restore started via Telegram for {snapshot_id}", f"telegram:{chat_id}")
    try:
        result = _call_core_api("POST", f"/api/system/backups/{snapshot_id}/restore", {}, timeout=300)
        if result and "_error" in result:
            fail_operation(op["operation_id"], result["_error"])
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"❌ Відновлення не вдався: {result['_error'][:200]}"
            )
        if isinstance(result, dict) and result.get("status") == "confirmation_required":
            token = str(result.get("confirmation_token") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
                fail_operation(op["operation_id"], "Backup Service не надав токен підтвердження")
                return TelegramAdapter().answer_callback(
                    callback_id, "❌ Backup Service не надав токен підтвердження відновлення."
                )
            advance_operation(op["operation_id"], "confirm", 25, "Підтвердження відновлення")
            audit_entry(op["operation_id"], "INTERNAL_CALL",
                        f"POST /api/system/backups/{snapshot_id}/restore/confirm", f"telegram:{chat_id}")
            result = _call_core_api(
                "POST", f"/api/system/backups/{snapshot_id}/restore/confirm?token={token}",
                {}, timeout=300)
            if result and "_error" in result:
                fail_operation(op["operation_id"], result["_error"])
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Відновлення не вдався: {result['_error'][:200]}"
                )
        if result and "status" in result:
            status = result.get("status")
            if status == "done":
                complete_operation(op["operation_id"], result)
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", "restore completed", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"✅ Відновлення завершено. ID операції: {op['operation_id'][:8]}"
                )
            fail_operation(op["operation_id"], f"Restore ended with status: {status}")
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"status={status}", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"❌ Відновлення завершилось зі статусом: {status}"
            )
        fail_operation(op["operation_id"], "Restore response missing status")
        return TelegramAdapter().answer_callback(
            callback_id, "❌ Відновлення не вдалося: відсутній статус у відповіді"
        )
    except Exception as error:
        fail_operation(op["operation_id"], str(error))
        audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"exception: {error}", f"telegram:{chat_id}")
        _queue_operation_notification(op, "❌", f"Відновлення не вдалося: {error}")
        return TelegramAdapter().answer_callback(callback_id, f"❌ Відновлення не вдалося: {error}")


def _sys_cancel(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    return TelegramAdapter().answer_callback(callback_id, "Операцію скасовано.")


def _sys_test_menu(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    keyboard = {
        "inline_keyboard": [
            [{"text": "⚡ Швидкий", "callback_data": "sys_test_quick"},
             {"text": "📋 Повний", "callback_data": "sys_test_full"}],
            [{"text": "🖥 Тест вузла", "callback_data": "sys_test_node"}],
            [{"text": "❌ Скасувати", "callback_data": "sys_cancel"}],
        ]
    }
    return TelegramAdapter().send_message(
        chat_id, "Оберіть тип тесту:", reply_markup=keyboard
    )


def _sys_test_quick(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    return _send_confirmation(
        chat_id,
        "⚡ Запустити швидкий тест (CORE API, PostgreSQL, Redis, Dispatcher, storage)?",
        "sys_test_quick_confirm", "sys_cancel",
    )


def _sys_test_full(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    return _send_confirmation(
        chat_id,
        "📋 Запустити повний тест (все включно з Worker Node)?",
        "sys_test_full_confirm", "sys_cancel",
    )


def _sys_test_quick_confirm(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    op, created = get_or_create_operation("test", f"telegram:{chat_id}", target="quick")
    if not created:
        return TelegramAdapter().answer_callback(
            callback_id, f"Тест вже в процесі (#{op['operation_id'][:8]})."
        )
    begin_operation(op["operation_id"], "Швидкий тест")
    audit_entry(op["operation_id"], "INTERNAL_CALL", "POST /api/system/test scope=quick", f"telegram:{chat_id}", confirmation="sys_test_quick_confirm")
    try:
        result = _call_core_api("POST", "/api/system/test", {"scope": "quick"}, timeout=30)
        if not isinstance(result, dict) or str(result.get("result", "")).upper() not in {"HEALTHY", "OK"}:
            fail_operation(op["operation_id"], "Self-test execution was not healthy")
            audit_entry(op["operation_id"], "RESULT", "Self-test failed", f"telegram:{chat_id}", confirmation="sys_test_quick_confirm")
            return TelegramAdapter().answer_callback(callback_id, "❌ Швидкий тест не пройдено.")
        if result and "_error" in result:
            fail_operation(op["operation_id"], str(result["_error"]))
            return TelegramAdapter().answer_callback(callback_id, f"❌ Швидкий тест не вдався: {result['_error']}")
        complete_operation(op["operation_id"], {"status": "ok", "scope": "quick", "result": result["result"]})
        audit_entry(op["operation_id"], "RESULT", "Self-test completed", f"telegram:{chat_id}", confirmation="sys_test_quick_confirm")
        return TelegramAdapter().answer_callback(callback_id, "✅ Швидкий тест пройдено.")
    except Exception as error:
        fail_operation(op["operation_id"], str(error))
        return TelegramAdapter().answer_callback(callback_id, f"❌ Швидкий тест не вдався: {error}")


def _sys_test_full_confirm(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    op, created = get_or_create_operation("test", f"telegram:{chat_id}", target="full")
    if not created:
        return TelegramAdapter().answer_callback(
            callback_id, f"Тест вже в процесі (#{op['operation_id'][:8]})."
        )
    begin_operation(op["operation_id"], "Повний тест")
    audit_entry(op["operation_id"], "INTERNAL_CALL", "POST /api/system/test scope=full", f"telegram:{chat_id}", confirmation="sys_test_full_confirm")
    try:
        result = _call_core_api("POST", "/api/system/test", {"scope": "full"}, timeout=60)
        if not isinstance(result, dict) or str(result.get("result", "")).upper() not in {"HEALTHY", "OK"}:
            fail_operation(op["operation_id"], "Self-test execution was not healthy")
            audit_entry(op["operation_id"], "RESULT", "Self-test failed", f"telegram:{chat_id}", confirmation="sys_test_full_confirm")
            return TelegramAdapter().answer_callback(callback_id, "❌ Повний тест не пройдено.")
        if result and "_error" in result:
            fail_operation(op["operation_id"], str(result["_error"]))
            return TelegramAdapter().answer_callback(callback_id, f"❌ Повний тест не вдався: {result['_error']}")
        complete_operation(op["operation_id"], {"status": "ok", "scope": "full", "result": result["result"]})
        audit_entry(op["operation_id"], "RESULT", "Self-test completed", f"telegram:{chat_id}", confirmation="sys_test_full_confirm")
        return TelegramAdapter().answer_callback(callback_id, "✅ Повний тест пройдено.")
    except Exception as error:
        fail_operation(op["operation_id"], str(error))
        return TelegramAdapter().answer_callback(callback_id, f"❌ Повний тест не вдався: {error}")


def _sys_test_node(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    nodes = _sync_internal_api("GET", "CORE_API_URL", "/api/nodes") or []
    if not nodes or not isinstance(nodes, list):
        return TelegramAdapter().answer_callback(callback_id, "Немає зареєстрованих вузлів.")
    return _send_nodes_list(chat_id, nodes, "sys_test_node_select")


def _sys_test_node_select(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    node_id = payload
    if not node_id or not re.fullmatch(r"[a-z0-9-]+", node_id):
        return TelegramAdapter().answer_callback(callback_id, "Некоректний вузол.")
    return _send_confirmation(
        chat_id, f"🧪 Запустити self-test для вузла {node_id}?",
        f"sys_test_node_confirm:{node_id}", "sys_cancel",
    )


def _sys_test_node_confirm(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    node_id = payload
    if not node_id:
        return TelegramAdapter().answer_callback(callback_id, "Некоректний вузол.")
    op, created = get_or_create_operation("test", f"telegram:{chat_id}", target=node_id)
    if not created:
        return TelegramAdapter().answer_callback(
            callback_id, f"Тест вже в процесі (#{op['operation_id'][:8]})."
        )
    begin_operation(op["operation_id"], f"Self-test вузла {node_id}")
    audit_entry(op["operation_id"], "INTERNAL_CALL", f"POST /api/nodes/{node_id}/actions action=self-test", f"telegram:{chat_id}")
    try:
        result = _call_core_api("POST", f"/api/nodes/{node_id}/actions", {"action": "self-test"}, timeout=60)
        if result and "_error" in result:
            fail_operation(op["operation_id"], str(result["_error"]))
            return TelegramAdapter().answer_callback(callback_id, f"❌ Self-test не вдався: {result['_error']}")
        complete_operation(op["operation_id"], {"status": "ok", "node_id": node_id})
        return TelegramAdapter().answer_callback(callback_id, f"✅ Self-test вузла {node_id} пройдено.")
    except Exception as error:
        fail_operation(op["operation_id"], str(error))
        return TelegramAdapter().answer_callback(callback_id, f"❌ Self-test не вдався: {error}")


# Action → handler dispatch table (replaces the if/elif chain).
SYSTEM_CALLBACK_DISPATCH: dict[str, Callable] = {
    "sys_status": _sys_status,
    "sys_status:menu": _sys_status,
    "sys_update": _sys_update,
    "sys_update_check": _sys_update_check,
    "sys_update_confirm": _sys_update_confirm,
    "sys_update_cancel": _sys_update_cancel,
    "sys_restart": _sys_restart,
    "sys_restart_core": _sys_restart_core,
    "sys_restart_core_confirm": _sys_restart_core_confirm,
    "sys_restart_worker": _sys_restart_worker,
    "sys_restart_worker_select": _sys_restart_worker_select,
    "sys_restart_worker_confirm": _sys_restart_worker_confirm,
    "sys_backup": _sys_backup,
    "sys_backup_confirm": _sys_backup_confirm,
    "sys_backup_cancel": _sys_backup_cancel,
    "sys_restore": _sys_restore,
    "sys_restore_select": _sys_restore_select,
    "sys_restore_confirm": _sys_restore_confirm,
    "sys_restore_execute": _sys_restore_execute,
    "sys_test": _sys_test_menu,
    "sys_test_quick": _sys_test_quick,
    "sys_test_full": _sys_test_full,
    "sys_test_quick_confirm": _sys_test_quick_confirm,
    "sys_test_full_confirm": _sys_test_full_confirm,
    "sys_test_node": _sys_test_node,
    "sys_test_node_select": _sys_test_node_select,
    "sys_test_node_confirm": _sys_test_node_confirm,
    "sys_cancel": _sys_cancel,
}


def _handle_system_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    """Dispatch a Telegram system callback to its action handler."""
    handler = SYSTEM_CALLBACK_DISPATCH.get(action)
    if handler is None:
        callback_id = str(callback.get("id", ""))
        return TelegramAdapter().answer_callback(callback_id, f"Невідома дія: {action}")
    return handler(callback, chat_id, action, payload)
