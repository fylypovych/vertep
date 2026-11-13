"""Telegram callback handlers (Issue #110 Phase D).

Extracted from ``core/app.py``.  ``_handle_system_callback`` uses a dispatch
table keyed by ``action`` instead of an if/elif chain.  The two
``for _ in range(30)`` wait loops were moved into ``_wait_for_core_healthy``
and ``_wait_for_worker_ready`` without changing their timeouts.

This module owns the Telegram entry points (callback and message routing,
polling).  The handlers live in ``common``, ``status``, ``system_ops`` and
``job_callbacks`` and are re-exported here, so ``core.telegram.handlers``
imports keep working.  A test that replaces a dependency must patch the module
that uses it.
"""

from __future__ import annotations

import os
from pathlib import Path

from adapters.telegram import (
    TelegramAdapter,
    TelegramPollingService,
    _integration_secret,
)
from core.api.jobs import (
    approve_job,
    cancel_job,
    publish_job,
    retry_job,
)
from core.api.workers import workers
from core.configuration import (
    BrandConfig,
    read_json,
)
from core.models import JobStatus
from core.operations import get_operation
from core.pipeline import _finalize_video_regenerate
from core.state import (
    _telegram_pending_brands,
    _telegram_pending_character,
    executor,
    logger,
    store,
    task_queue,
)
from core.telegram_callbacks import (
    claim_callback,
    complete_callback,
)
from core.telegram_store import (
    is_admin_chat,
    is_allowed_chat,
)

from .common import (  # noqa: F401
    _ABS_PATH_RE,
    _ACTION_TO_OP_TYPE,
    _PROCESS_STARTED_AT,
    _admin_chat_ids,
    _call_backup_api,
    _call_core_api,
    _host_status_is_stale,
    _is_admin_chat,
    _link_callback_to_operation,
    _operation_started_before_process,
    _queue_operation_notification,
    _record_operation_field,
    _telegram_system_callbacks,
    _telegram_system_restore,
    _wait_for_core_healthy,
    _wait_for_worker_ready,
)
from .status import (  # noqa: F401
    _deliver_pending_telegram_notifications,
    _format_jobs_summary,
    _format_last_backup,
    _format_operation,
    _format_workers_summary,
    _reconcile_restart_operations,
    _reconcile_update_operations,
    _recover_stale_update_operations,
    _release_notes_for,
    _send_backups_list,
    _send_confirmation,
    _send_nodes_list,
    _send_system_menu,
    _system_status_text,
    _update_target_version,
)
from .system_ops import (  # noqa: F401
    SYSTEM_CALLBACK_DISPATCH,
    _handle_system_callback,
    _sys_backup,
    _sys_backup_cancel,
    _sys_backup_confirm,
    _sys_cancel,
    _sys_restart,
    _sys_restart_core,
    _sys_restart_core_confirm,
    _sys_restart_worker,
    _sys_restart_worker_confirm,
    _sys_restart_worker_select,
    _sys_restore,
    _sys_restore_confirm,
    _sys_restore_execute,
    _sys_restore_select,
    _sys_status,
    _sys_test_full,
    _sys_test_full_confirm,
    _sys_test_menu,
    _sys_test_node,
    _sys_test_node_confirm,
    _sys_test_node_select,
    _sys_test_quick,
    _sys_test_quick_confirm,
    _sys_update,
    _sys_update_cancel,
    _sys_update_check,
    _sys_update_confirm,
)
from .job_callbacks import (  # noqa: F401
    _create_job_from_telegram,
    _generate_storyboard_and_notify,
    _handle_approve_job,
    _handle_brand_selection,
    _handle_character_selection,
    _handle_publish_all,
    _handle_publish_channel,
    _handle_reject_job,
    _handle_script_callback,
    _handle_storyboard_callback,
    _handle_video_callback,
    _publish_to_channel,
    _queue_storyboard,
    _regenerate_script_and_notify,
    _regenerate_storyboard_and_notify,
    _sanitize_creation_error,
    _save_telegram_attachments,
    _send_approval_request,
    _send_character_selection,
)

telegram_polling_service = None


def _handle_telegram_callback(callback: dict) -> dict:
    """Dispatch a Telegram callback query to the appropriate handler.

    System actions are handled in this module; all other actions delegate
    to the local sub-handlers and their explicit dependencies.
    """
    data = str(callback.get("data", ""))
    action, _, payload = data.partition(":")
    callback_id = str(callback.get("id", ""))
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", "unknown"))
    if not (is_admin_chat(chat_id) or is_allowed_chat(chat_id)):
        return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено.")

    system_actions = {
        "sys_status", "sys_update", "sys_update_check", "sys_update_confirm",
        "sys_update_cancel",
        "sys_restart", "sys_restart_core", "sys_restart_core_confirm",
        "sys_restart_worker", "sys_restart_worker_select", "sys_restart_worker_confirm",
        "sys_backup", "sys_backup_confirm", "sys_backup_cancel",
        "sys_restore", "sys_restore_select", "sys_restore_confirm",
        "sys_restore_execute", "sys_restore_cancel",
        "sys_test", "sys_test_quick", "sys_test_quick_confirm",
        "sys_test_full", "sys_test_full_confirm", "sys_test_node",
        "sys_test_node_select", "sys_test_node_confirm", "sys_cancel",
    }
    if action in system_actions:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(
                callback_id, "Доступ заборонено: лише для адміністраторів."
            )
        _record, claimed = claim_callback(
            callback_id, actor=f"telegram:{chat_id}", action=action,
            target=payload or None,
        )
        if not claimed:
            return TelegramAdapter().answer_callback(
                callback_id, "Операцію вже оброблено."
            )
        try:
            result = _handle_system_callback(callback, chat_id, action, payload)
        except Exception:
            complete_callback(callback_id, status="FAILED")
            raise
        _link_callback_to_operation(callback_id, action, payload, chat_id)
        complete_callback(callback_id)
        return result

    if action == "select_brand":
        return _handle_brand_selection(callback, chat_id, payload)
    elif action == "select_character":
        return _handle_character_selection(callback, chat_id, payload)
    elif action in {"select_workflow", "wf"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: лише для адміністраторів.")
        from .workflows import select_workflow, handle_action
        try:
            if action == "select_workflow":
                pending_data = _telegram_pending_character.get(chat_id)
                if not pending_data:
                    raise ValueError("Сесію закрито")
                return select_workflow(callback, chat_id, payload, pending_data)
            return handle_action(callback, chat_id, payload)
        except (ValueError, KeyError) as error:
            return TelegramAdapter().answer_callback(callback_id, str(error))
    elif action in {"approve", "reject"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(
                callback_id, "Доступ заборонено: лише для адміністраторів."
            )
        _record, claimed = claim_callback(
            callback_id, actor=f"telegram:{chat_id}", action=action, target=payload or None
        )
        if not claimed:
            return TelegramAdapter().answer_callback(
                callback_id, "Операцію вже оброблено."
            )
        try:
            result = (_handle_approve_job(callback, chat_id, payload)
                      if action == "approve"
                      else _handle_reject_job(callback, chat_id, payload))
        except Exception:
            complete_callback(callback_id, status="FAILED")
            raise
        complete_callback(callback_id)
        return result
    elif action in {"publish_channel", "publish_all"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(
                callback_id, "Доступ заборонено: лише для адміністраторів."
            )
        _record, claimed = claim_callback(
            callback_id, actor=f"telegram:{chat_id}", action=action, target=payload or None
        )
        if not claimed:
            return TelegramAdapter().answer_callback(
                callback_id, "Операцію вже оброблено."
            )
        try:
            result = (_handle_publish_channel(callback, chat_id, payload)
                      if action == "publish_channel"
                      else _handle_publish_all(callback, chat_id, payload))
        except Exception:
            complete_callback(callback_id, status="FAILED")
            raise
        complete_callback(callback_id)
        return result
    elif action in {"sb_ok", "sb_regen", "sb_edit", "sb_reject",
                    "sb_img_ok", "sb_img_regen", "sb_img_edit", "sb_img_scene"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(
                callback_id, "Доступ заборонено: лише для адміністраторів."
            )
        return _handle_storyboard_callback(callback, chat_id, action, payload)
    elif action in {"sc_ok", "sc_regen", "sc_edit", "sc_reject"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(
                callback_id, "Доступ заборонено: лише для адміністраторів."
            )
        return _handle_script_callback(callback, chat_id, action, payload)
    elif action in {"vid_ok", "vid_regen", "vid_edit", "vid_reject"}:
        return _handle_video_callback(callback, chat_id, action, payload)

    job_id = payload
    job = store.jobs.get(job_id)
    if job and action == "cancel":
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(
                callback_id, "Доступ заборонено: лише для адміністраторів."
            )
        cancel_job(job_id)
    text = f"{job_id}: {job.status.value}" if job else "Job not found"
    return TelegramAdapter().answer_callback(callback_id, text)


def _handle_telegram_message(chat_id: str, source_id: str, text: str, message: dict) -> dict:
    """Dispatch an inbound Telegram message to the appropriate handler."""
    if text and not text.startswith("/") and is_admin_chat(chat_id):
        from .workflows import apply_revision
        try:
            revised = apply_revision(chat_id, text)
        except ValueError as error:
            return TelegramAdapter().send_message(chat_id, str(error))
        if revised is not None:
            return revised
    if text == "/start":
        return TelegramAdapter().send_message(
            chat_id, "Vertep Bot підключений.\nНадішліть тему для створення контенту."
        )
    if text in {"/system", "/admin"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().send_message(chat_id, "Доступ заборонено.")
        return _send_system_menu(chat_id)
    if text.startswith("/operation "):
        operation_id = text.split(maxsplit=1)[1].strip()
        operation = get_operation(operation_id)
        if not operation:
            return TelegramAdapter().send_message(chat_id, "Операцію не знайдено.")
        return TelegramAdapter().send_message(
            chat_id, _format_operation(operation))
    if text.startswith("/status"):
        active = sum(job.status not in {JobStatus.READY, JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.CANCELLED}
                     for job in store.jobs.values())
        return TelegramAdapter().send_message(
            chat_id, f"Jobs: {len(store.jobs)}\nActive: {active}\nQueue: {task_queue.depth()}")
    if text.startswith("/jobs"):
        latest = list(store.jobs.values())[-10:]
        summary = "\n".join(f"{job.job_id} {job.status.value} — {job.topic[:50]}" for job in latest) or "No jobs"
        return TelegramAdapter().send_message(chat_id, summary)
    if text.startswith("/workers"):
        summary = "\n".join(f"{worker['node_name']} {worker['status']} {worker.get('gpu_name','')}"
                             for worker in workers()) or "No workers"
        return TelegramAdapter().send_message(chat_id, summary)
    if text.startswith("/job "):
        job = store.jobs.get(text.split(maxsplit=1)[1].strip())
        if not job:
            return TelegramAdapter().send_message(chat_id, "Job not found")
        scenes = "\n".join(f"{scene.scene_id}: {scene.status.value} ({len(scene.attempts)} attempt(s))"
                            for scene in job.scenes) or "Scenes are not planned yet"
        return TelegramAdapter().send_message(chat_id, f"{job.job_id}: {job.status.value}\n{scenes}")
    if any(text.startswith(command + " ") for command in ("/retry", "/cancel", "/approve", "/publish")):
        if not is_admin_chat(chat_id):
            return TelegramAdapter().send_message(chat_id, "Доступ заборонено.")
        command, job_id = text.split(maxsplit=1)
        job = store.jobs.get(job_id.strip())
        if not job:
            return TelegramAdapter().send_message(chat_id, "Job not found")
        if command == "/retry":
            retry_job(job.job_id)
        elif command == "/cancel":
            cancel_job(job.job_id)
        elif command == "/approve":
            approve_job(job.job_id)
        else:
            publish_job(job.job_id)
        return TelegramAdapter().send_message(chat_id, f"{job.job_id}: {job.status.value}")
    revision_job = next((job for job in store.jobs.values()
                         if job.storyboard_revision_chat_id == chat_id), None)
    if revision_job and revision_job.storyboard_revision_version:
        version = revision_job.storyboard_revision_version
        revision_job.storyboard_revision_chat_id = None
        revision_job.storyboard_revision_version = None
        executor.submit(_regenerate_storyboard_and_notify,
                        revision_job.job_id, version, chat_id, text)
        return TelegramAdapter().send_message(chat_id, "✍️ Правки прийнято. Генерую нову версію…")
    # --- Script revision text ---
    script_rev_job = next((job for job in store.jobs.values()
                           if job.script_revision_chat_id == chat_id
                           and job.script_revision_pending), None)
    if script_rev_job:
        script_rev_job.script_revision_chat_id = None
        script_rev_job.script_revision_pending = False
        from core.pipeline import request_script_revision, regenerate_script
        try:
            request_script_revision(store, script_rev_job, text, f"telegram:{chat_id}")
            executor.submit(_regenerate_script_and_notify,
                            script_rev_job.job_id, chat_id, text)
            return TelegramAdapter().send_message(chat_id, "✍️ Правки до сценарію прийнято. Генерую нову версію…")
        except Exception as error:
            return TelegramAdapter().send_message(chat_id, f"❌ Помилка: {error}")
    # --- Video revision text ---
    video_rev_job = next((job for job in store.jobs.values()
                          if job.video_revision_chat_id == chat_id
                          and job.video_revision_pending), None)
    if video_rev_job:
        video_rev_job.video_revision_chat_id = None
        video_rev_job.video_revision_pending = False
        from core.pipeline import request_video_revision
        try:
            revised = request_video_revision(store, video_rev_job, text, f"telegram:{chat_id}")
            if getattr(revised, "video_revision_upstream", None):
                return TelegramAdapter().send_message(
                    chat_id, "✍️ Правки до відео прийнято. Оновлюю сценарій і розкадровку…")
            executor.submit(_finalize_video_regenerate, revised)
            return TelegramAdapter().send_message(chat_id, "✍️ Правки до відео прийнято. Перезбираю…")
        except Exception as error:
            return TelegramAdapter().send_message(chat_id, f"❌ Помилка: {error}")
    attachments = {key: message.get(key) for key in ("photo", "video", "document", "audio") if message.get(key)}
    _telegram_pending_brands[chat_id] = {"text": text, "source_id": source_id, "message": message, "attachments": attachments}
    brands_dir = Path(os.getenv("BRANDS_ROOT", "brands"))
    brands = []
    for path in brands_dir.glob("*/brand.json"):
        try:
            brands.append(BrandConfig.model_validate(read_json(path)))
        except ValueError:
            continue
    if not brands:
        return _send_character_selection(chat_id, None)
    keyboard = {"inline_keyboard": [
        [{"text": f"📁 {brand.name}", "callback_data": f"select_brand:{brand.id}"}] for brand in brands
    ]}
    TelegramAdapter().send_message(chat_id, "Оберіть бренд для цього завдання:", keyboard)
    return {"status": "brand_selection", "brands": [b.id for b in brands]}


def _start_telegram_polling() -> None:
    global telegram_polling_service
    token = _integration_secret("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        return
    if os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() != "true":
        return
    telegram_polling_service = TelegramPollingService(
        token=token,
        on_update=_process_telegram_update,
    )
    telegram_polling_service.start()
    _reconcile_update_operations()
    _deliver_pending_telegram_notifications()
    logger.info("Telegram polling started")


def _restart_telegram_polling() -> None:
    _stop_telegram_polling()
    _start_telegram_polling()


def _stop_telegram_polling() -> None:
    global telegram_polling_service
    if telegram_polling_service is not None:
        telegram_polling_service.stop()
        telegram_polling_service = None
        logger.info("Telegram polling stopped")


def _process_telegram_update(update: dict) -> None:
    callback = update.get("callback_query")
    if callback:
        _handle_telegram_callback(callback)
        return
    message = update.get("message") or {}
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        logger.debug("Telegram update skipped: no text")
        return
    chat_id = str(message.get("chat", {}).get("id", "unknown"))
    if not is_allowed_chat(chat_id):
        logger.warning("Telegram message rejected: chat_id not allowed", extra={"chat_id": chat_id})
        try:
            TelegramAdapter().send_message(chat_id, "Доступ заборонено.")
        except Exception:
            pass
        return
    source_id = str(message.get("message_id", ""))
    if text == "/start":
        try:
            TelegramAdapter().send_message(chat_id, "Vertep Bot підключений.\nНадішліть тему для створення контенту.")
        except Exception:
            pass
        return
    _handle_telegram_message(chat_id, source_id, text, message)
