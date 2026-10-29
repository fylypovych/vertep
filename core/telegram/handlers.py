"""Telegram callback handlers (Issue #110 Phase D).

Extracted from ``core/app.py``.  ``_handle_system_callback`` uses a dispatch
table keyed by ``action`` instead of an if/elif chain.  The two
``for _ in range(30)`` wait loops were moved into ``_wait_for_core_healthy``
and ``_wait_for_worker_ready`` without changing their timeouts.
"""

from __future__ import annotations
from core.operations import OperationStatus, pending_notifications, acknowledge_notification
from core.update_manager import pending_request_ids, mark_request_failed
from core.runtime_identity import CORE_RUNTIME_INSTANCE_ID
from core.api._internal import _sync_internal_api, _internal_auth_headers
from core.pipeline import _finalize_video_regenerate, _notify_video_regeneration, _collect_reassembly_inputs
from adapters.telegram import TelegramAdapter
from adapters.telegram import TelegramPollingService
from adapters.telegram import _integration_secret
from core.api.job_helpers import _prepare_and_dispatch
from core.api.jobs import approve_job
from core.api.jobs import cancel_job
from core.api.jobs import publish_job
from core.api.jobs import retry_job
from core.api.nodes import nodes
from core.api.observability import health
from core.api.workers import workers
from core.artifacts import register_artifact
from core.configuration import BrandConfig
from core.configuration import load_character
from core.configuration import read_json
from core.dispatcher import available_worker
from core.models import JobStatus
from core.models import StageName
from core.models import StageStatus
from core.operations import add_pending_notification
from core.operations import advance_operation
from core.operations import audit_entry
from core.operations import begin_operation
from core.operations import complete_operation
from core.operations import fail_operation
from core.operations import get_operation
from core.operations import get_or_create_operation
from core.operations import is_operation_in_progress
from core.operations import link_operation_request
from core.operations import list_operations
from core.operations import set_operation_fields
from core.orchestration import transition_stage
from core.pipeline import regenerate_script
from core.state import _telegram_pending_brands
from core.state import _telegram_pending_character
from core.state import executor
from core.state import logger
from core.state import store
from core.state import task_queue
from core.storyboard import StoryboardConflict
from core.storyboard import StoryboardService
from core.system_state import get_system_state
from core.telegram_callbacks import DurableCallbackSet
from core.telegram_callbacks import claim_callback
from core.telegram_callbacks import complete_callback
from core.telegram_callbacks import link_operation
from core.telegram_callbacks import set_callback_target
from core.telegram_store import get_admin_chat_ids
from core.telegram_store import get_allowed_chat_ids
from core.telegram_store import is_admin_chat
from core.update_manager import request_update
from core.update_manager import update_status
from core.version import application_version
from datetime import datetime
from datetime import timezone
from fastapi import HTTPException
from pathlib import Path
import hashlib
import httpx
import json
import os
import psutil
import re
import time
telegram_polling_service = None

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable

from fastapi import HTTPException

from adapters.telegram import TelegramPollingService
from core.api.job_helpers import _enqueue_publish_task, _has_publisher_worker
from core.models import JobStatus, StageName, StageStatus
from core.operations import (
    add_pending_notification, advance_operation, audit_entry, begin_operation,
    complete_operation, fail_operation, get_or_create_operation,
    is_operation_in_progress, link_operation_request, list_operations,
    set_operation_fields,
)
from core.models import JobStatus
from core.state import executor, logger, store, task_queue
from core.system_state import get_system_state
from core.telegram_callbacks import set_callback_target
from core.telegram_store import get_admin_chat_ids, is_admin_chat
from core.update_manager import request_update, update_status
from core.version import application_version
from core.configuration import BrandConfig, read_json
from core.storyboard import StoryboardConflict, StoryboardService
from core.orchestration import transition_stage


# ---------------------------------------------------------------------------
# Local operation helpers (mirrored from core/app.py so this module is
# self-contained and does not import core.app).
# ---------------------------------------------------------------------------

def _record_operation_field(operation_id: str, **fields) -> None:
    """Persist correlation fields of an operation without breaking the flow."""
    try:
        set_operation_fields(operation_id, **fields)
    except KeyError:
        logger.warning(
            "Operation %s is not persisted; correlation fields were dropped",
            str(operation_id)[:8],
        )


def _queue_operation_notification(operation: dict, marker: str, message: str) -> None:
    """Queue a durable Telegram notification for a finished operation."""
    requested_by = str(operation.get("requested_by") or "")
    if not requested_by.startswith("telegram:"):
        return
    chat_id = requested_by.split(":", 1)[1]
    add_pending_notification(chat_id, f"{marker} {message}")


# ---------------------------------------------------------------------------
# Wait helpers (extracted from the two ``for _ in range(30)`` loops).
# ---------------------------------------------------------------------------

def _wait_for_core_healthy(previous_instance_id: str) -> bool:
    """Wait up to ~60 s for CORE to report HEALTHY with a new instance id."""
    for _ in range(30):  # up to 60 seconds
        time.sleep(2)
        health = _call_core_api("GET", "/api/health", timeout=5)
        if (health and health.get("status") == "healthy"
                and health.get("runtime_instance_id") != previous_instance_id):
            return True
    return False


def _wait_for_worker_ready(node_id: str, restart_operation_id: str) -> dict | None:
    """Wait up to ~60 s for a worker to report READY with a matching restart ack."""
    for _ in range(30):  # up to 60 seconds
        time.sleep(2)
        node = _call_core_api("GET", f"/api/nodes/{node_id}", timeout=5)
        restart_ack = ((node or {}).get("update_state") or {}).get("restart_ack") or {}
        if (node and node.get("status") == "READY"
                and restart_ack.get("operation_id") == restart_operation_id):
            return node
    return None


# ---------------------------------------------------------------------------
# Internal API helpers use the shared HTTP owner directly.
# ---------------------------------------------------------------------------















# ---------------------------------------------------------------------------
# Telegram formatting helpers.
# ---------------------------------------------------------------------------

def _format_operation(operation: dict) -> str:
    status = operation.get("status", "UNKNOWN")
    op_type = operation.get("type", "?")
    op_id = str(operation.get("operation_id", ""))[:8]
    progress = operation.get("progress", 0)
    phase = operation.get("current_phase") or "—"
    error = operation.get("error")
    from core.logging_config import secret_redact
    error = secret_redact(error) if error else None
    lines = [
        f"Операція {op_type} (#{op_id})",
        f"Статус: {status} · прогрес {progress}% · фаза: {phase}",
    ]
    if error:
        lines.append(f"Помилка: {error}")
    return "\n".join(lines)


def _system_status_text() -> str:
    from core.health_checks import check_postgres, check_redis, check_core_api
    _reconcile_update_operations()
    state = get_system_state()
    update = {}
    try:
        update = update_status() or {}
    except Exception:
        pass
    state_name = state.get("state", "NORMAL")
    update_state = update.get("state", "IDLE")
    current = update.get("current_version") or "?"

    try:
        import psutil
        boot_time = psutil.boot_time()
        uptime_sec = int(time.time() - boot_time)
        days, rem = divmod(uptime_sec, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        uptime_str = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"
    except Exception:
        uptime_str = "?"

    pg_ok, pg_msg = check_postgres()
    redis_ok, redis_msg = check_redis()
    core_url = os.getenv("CORE_ADDRESS", "")
    core_ok, core_msg = check_core_api(core_url) if core_url else (None, "not configured")

    dispatcher_ok = "running"
    try:
        from core.dispatcher import available_worker
        workers = list(store.workers.values())
        if workers:
            _ = available_worker(workers, type("Job", (), {
                "job_id": "_health", "topic": "_health", "character_id": "",
                "priority": 1, "status": JobStatus.NEW,
                "created_at": __import__("core.state", fromlist=["utc_now"]).utc_now(),
            })())
    except Exception:
        dispatcher_ok = "error"

    lines = [
        f"Система: {state_name}",
        f"Uptime: {uptime_str}",
        f"PostgreSQL: {'✅' if pg_ok else '❌' if pg_ok is False else '⚠️'}",
        f"Redis: {'✅' if redis_ok else '❌' if redis_ok is False else '⚠️'}",
        f"CORE API: {'✅' if core_ok else '❌' if core_ok is False else '⚠️'}",
        f"Dispatcher: {dispatcher_ok}",
        f"Оновлення: {update_state} (поточна {current})",
    ]
    available = update.get("available_version")
    if available:
        lines.append(f"Доступна версія: {available}")
    lines.append(_format_workers_summary())
    lines.append(_format_jobs_summary())
    lines.append(_format_last_backup())
    operations = list_operations(5)
    if operations:
        lines.append("Останні операції:")
        for op in operations:
            lines.append(f"  • {_format_operation(op)}")
    return "\n".join(lines)


def _format_workers_summary() -> str:
    nodes = _sync_internal_api("GET", "CORE_API_URL", "/api/nodes") \
        or _sync_internal_api("GET", "BACKUP_URL", "/api/nodes")
    if not nodes or not isinstance(nodes, list):
        return "Вузли: 0/0 online"
    online = sum(1 for n in nodes if (n.get("runtime_status") or n.get("status")) == "ONLINE")
    return f"Вузли: {online}/{len(nodes)} online"


def _format_jobs_summary() -> str:
    data = _sync_internal_api("GET", "CORE_API_URL", "/api/jobs",
                              {"per_page": 100, "page": 1}) or {}
    jobs = data if isinstance(data, list) else data.get("jobs", [])
    if not isinstance(jobs, list):
        return "Jobs: 0 active / 0 queued / 0 failed"
    active = {"PROCESSING", "SCRIPTING", "ASSET_GENERATION", "VIDEO_GENERATION",
              "ASSEMBLY", "PUBLISHING"}
    queued = {"NEW", "WAITING_FOR_SYSTEM"}
    failed = {"FAILED"}
    counts = {"active": 0, "queued": 0, "failed": 0}
    for job in jobs:
        status = str(job.get("status", "")).upper()
        if status in active:
            counts["active"] += 1
        elif status in queued:
            counts["queued"] += 1
        elif status in failed:
            counts["failed"] += 1
    return (f"Jobs: {counts['active']} active / {counts['queued']} queued "
            f"/ {counts['failed']} failed")


def _format_last_backup() -> str:
    data = _sync_internal_api("GET", "BACKUP_URL", "/snapshots")
    snapshots = (data or {}).get("snapshots", []) if isinstance(data, dict) else []
    if not snapshots:
        return "Останній backup: unavailable"
    latest = snapshots[0]
    return (f"Останній backup: {latest.get('snapshot_id', '?')} "
            f"({latest.get('created_at', '?')})")


# ---------------------------------------------------------------------------
# Operation helpers.
# ---------------------------------------------------------------------------

def _recover_stale_update_operations() -> list[str]:
    """Fail update operations whose host request was lost (Issue #174.3).

    A CORE process that dies while the host agent works on a request leaves
    the operation in ``RUNNING`` forever and the host status in
    ``PENDING``/``RUNNING``.  Every later confirmation is then refused as
    "already in progress", so the failure path has to become terminal and a
    retry has to stay possible.
    """
    try:
        status = update_status() or {}
    except Exception:
        return []
    state = str(status.get("state", "")).upper()
    host_request = status.get("request_id")
    queued = pending_request_ids()
    host_stale = _host_status_is_stale(status)
    recovered: list[str] = []
    for operation in list_operations(200):
        if operation.get("type") != "update":
            continue
        if operation.get("status") not in {OperationStatus.QUEUED.value,
                                           OperationStatus.RUNNING.value}:
            continue
        external = operation.get("external_request_id")
        if external:
            if external in queued:
                continue
            if external == host_request and state in {"PENDING", "RUNNING"}:
                if not host_stale:
                    continue
                mark_request_failed(external, "Update host agent stopped responding")
        elif not _operation_started_before_process(operation):
            continue
        fail_operation(
            operation["operation_id"],
            "Stale update operation recovered: the host request is no longer active",
        )
        recovered.append(operation["operation_id"])
    return recovered


def _reconcile_restart_operations() -> list[str]:
    """Finish restart operations whose effect survived a CORE process restart.

    A restart effect is durable: the node records the acknowledgement of its
    new runtime instance and the CORE records its own instance id.  After a
    process restart the operation has to reach a terminal state instead of
    blocking every later restart request (Issue #174.4).
    """
    finished: list[str] = []
    for operation in list_operations(200):
        if operation.get("type") != "restart":
            continue
        if operation.get("status") not in {OperationStatus.QUEUED.value,
                                           OperationStatus.RUNNING.value}:
            continue
        target = str(operation.get("target") or "")
        if target == "core":
            previous = str(operation.get("previous_runtime_instance_id") or "")
            if not previous or previous == CORE_RUNTIME_INSTANCE_ID:
                continue
            complete_operation(operation["operation_id"],
                               {"status": "healthy", "target": "core",
                                "runtime_instance_id": CORE_RUNTIME_INSTANCE_ID,
                                "message": "CORE перезапущено"})
            _queue_operation_notification(
                operation, "✅",
                f"Перезапуск CORE #{operation['operation_id'][:8]} завершено.")
            finished.append(operation["operation_id"])
            continue
        worker = store.workers.get(target) or {}
        acknowledgement = worker.get("restart_ack") or {}
        if (worker.get("status") == "READY"
                and acknowledgement.get("operation_id")
                and acknowledgement.get("operation_id") == operation.get("restart_operation_id")):
            complete_operation(operation["operation_id"],
                               {"status": "ready", "node_id": target,
                                "restart_operation_id": acknowledgement.get("operation_id"),
                                "runtime_instance_id": acknowledgement.get("runtime_instance_id"),
                                "message": f"Worker {target} перезапущено успішно"})
            _queue_operation_notification(
                operation, "✅",
                f"Вузол {target} перезапущено і READY. ID: {operation['operation_id'][:8]}")
            finished.append(operation["operation_id"])
    return finished


def _reconcile_update_operations() -> None:
    """Correlate durable update-agent results with Telegram operations."""
    try:
        status = update_status() or {}
    except Exception:
        return
    request_id = status.get("request_id")
    state = str(status.get("state", "")).upper()
    if not request_id:
        return
    for operation in list_operations(200):
        if (operation.get("type") != "update"
                or operation.get("external_request_id") != request_id
                or operation.get("status") not in {"QUEUED", "RUNNING"}):
            continue
        # Phase trail: the host agent reports its phase in ``status.json``.
        # Mirroring it into the durable operation keeps the Telegram status
        # output truthful while the update is still running (Issue #174.3).
        phase = str(status.get("phase") or "").strip()
        if phase and phase != operation.get("current_phase"):
            try:
                progress = int(status.get("progress") or 0)
            except (TypeError, ValueError):
                progress = 0
            advance_operation(operation["operation_id"], phase, progress,
                              status.get("message"))
        if state not in {"SUCCEEDED", "FAILED", "ROLLED_BACK"}:
            continue
        if state == "SUCCEEDED":
            complete_operation(operation["operation_id"], status)
            marker = "✅"
        else:
            fail_operation(operation["operation_id"], status.get("message") or state)
            marker = "❌"
        requested_by = str(operation.get("requested_by") or "")
        if requested_by.startswith("telegram:"):
            chat_id = requested_by.split(":", 1)[1]
            version = status.get("current_version") or status.get("available_version")
            suffix = f" ({version})" if version else ""
            add_pending_notification(
                chat_id,
                f"{marker} Операція оновлення #{operation['operation_id'][:8]}{suffix} завершена: "
                f"{status.get('message') or state}",
            )


def _release_notes_for(version: str) -> list[str]:
    """Release notes of a target version for the update confirmation.

    The notes come from the release artefacts that ship with the source tree
    (``releases/<version>.md`` or the matching ``CHANGELOG.md`` block).  A
    deployment without them confirms the version transition only.
    """
    if not version:
        return []
    root = Path(__file__).resolve().parents[1]
    text = ""
    try:
        text = (root / "releases" / f"{version}.md").read_text(encoding="utf-8")
    except OSError:
        text = ""
    if not text:
        try:
            changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        except OSError:
            return []
        pattern = re.compile(
            rf"(?ms)^## ПРАВИЛЬНА НАЗВА: {re.escape(version)}\s*\n(.*?)(?=^## |\Z)")
        match = pattern.search(changelog)
        text = match.group(1) if match else ""
    return [line.strip() for line in text.splitlines() if line.strip().startswith("-")][:10]


def _update_target_version() -> str | None:
    """Version the update operation would move the system to."""
    try:
        status = update_status() or {}
    except Exception:
        return None
    return status.get("available_version") or None


def _deliver_pending_telegram_notifications() -> None:
    """Retry final operation delivery after CORE/process restarts."""
    adapter = TelegramAdapter()
    for notification in pending_notifications():
        try:
            result = adapter.send_message(notification["chat_id"], notification["message"])
        except Exception as error:
            logger.warning("Telegram operation notification delivery failed: %s", error)
            continue
        if isinstance(result, dict) and result.get("status") == "STUB":
            # Nothing was delivered (the bot token is not configured), so the
            # notification has to stay queued for the next attempt
            # (Issue #174.2).
            logger.warning("Telegram operation notification was not delivered: %s",
                           result.get("reason"))
            continue
        acknowledge_notification(notification["notification_id"])


def _send_confirmation(chat_id: str, message: str, confirm_data: str,
                       cancel_data: str = "sys_cancel"):
    _Adapter = TelegramAdapter
    _Adapter().send_message(
        chat_id,
        message,
        reply_markup={"inline_keyboard": [
            [{"text": "✅ Підтвердити", "callback_data": confirm_data}],
            [{"text": "❌ Скасувати", "callback_data": cancel_data}],
        ]},
    )


def _send_system_menu(chat_id: str) -> dict:
    menu = {
        "inline_keyboard": [
            [
                {"text": "📊 Статус", "callback_data": "sys_status:menu"},
                {"text": "🔄 Оновлення", "callback_data": "sys_update:menu"},
                {"text": "🔁 Рестарт", "callback_data": "sys_restart:menu"},
            ],
            [
                {"text": "💾 Резервна копія", "callback_data": "sys_backup:menu"},
                {"text": "♻️ Відновлення", "callback_data": "sys_restore:menu"},
                {"text": "🧪 Тест", "callback_data": "sys_test:menu"},
            ],
        ]
    }
    return TelegramAdapter().send_message(chat_id, "🛠 Керування системою:", reply_markup=menu)


def _send_backups_list(chat_id: str, snapshots: list) -> dict:
    _Adapter = TelegramAdapter
    lines = [f"⚠️ ПЕРШЕ підтвердження відновлення\nОберіть snapshot:"]
    keyboard = []
    for snap in snapshots[:10]:
        sid = snap.get("snapshot_id", "?")
        created = snap.get("created_at", "?")
        size = snap.get("size", "?")
        keyboard.append([{"text": f"💾 {sid[:18]} ({created[:10]})",
                          "callback_data": f"sys_restore_select:{sid}"}])
    keyboard.append([{"text": "❌ Скасувати", "callback_data": "sys_cancel"}])
    return _Adapter().send_message(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": keyboard})


def _send_nodes_list(chat_id: str, nodes: list, confirm_prefix: str) -> dict:
    _Adapter = TelegramAdapter
    keyboard = []
    for node in nodes:
        node_id = node.get("node_id") or node.get("node_name", "?")
        status = node.get("status", "?")
        keyboard.append([{"text": f"🖥 {node_id} ({status})",
                          "callback_data": f"{confirm_prefix}:{node_id}"}])
    keyboard.append([{"text": "❌ Скасувати", "callback_data": "sys_cancel"}])
    return _Adapter().send_message(chat_id, "Оберіть вузол:", reply_markup={"inline_keyboard": keyboard})


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


def _handle_telegram_callback(callback: dict) -> dict:
    """Dispatch a Telegram callback query to the appropriate handler.

    System actions are handled in this module; all other actions delegate
    to the local sub-handlers and their explicit dependencies.
    """
    data = str(callback.get("data", ""))
    action, _, payload = data.partition(":")
    callback_id = str(callback.get("id", ""))
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", "unknown"))

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
    allowed = get_allowed_chat_ids()
    admin_ids = set(get_admin_chat_ids())
    if allowed and chat_id not in allowed and chat_id not in admin_ids:
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


_ABS_PATH_RE = re.compile(r"(?<![:/])(?<!\w)(?:[A-Za-z]:[\\/]|/)[^\s'\";)]*")


def _sanitize_creation_error(error: Exception) -> str:
    from core.logging_config import secret_redact
    text = str(error) or "невідома помилка"
    text = secret_redact(text)
    text = _ABS_PATH_RE.sub("[path]", text)
    return text[:200]


def _handle_brand_selection(callback: dict, chat_id: str, brand_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    pending = _telegram_pending_brands.get(chat_id)
    if not pending:
        return TelegramAdapter().answer_callback(callback_id, "Сесію закрито. Почніть спочатку.")
    TelegramAdapter().answer_callback(callback_id, f"Бренд обрано. Тепер оберіть персонажа.")
    _telegram_pending_brands.pop(chat_id, None)
    _telegram_pending_character[chat_id] = {"pending": pending, "brand_id": brand_id}
    return _send_character_selection(chat_id, brand_id)


def _send_character_selection(chat_id: str, brand_id: str) -> dict:
    characters_root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    character_dirs = sorted(characters_root.glob("*"), key=lambda p: p.name)
    characters = []
    for char_dir in character_dirs:
        char_config_path = char_dir / "character.json"
        if char_config_path.exists():
            try:
                config = read_json(char_config_path)
                characters.append({"id": char_dir.name, "name": config.get("name", char_dir.name)})
            except (ValueError, OSError):
                continue
    if not characters:
        character_id = os.getenv("TELEGRAM_DEFAULT_CHARACTER", "did_samogon")
        pending = _telegram_pending_character.get(chat_id, {}).get("pending", {})
        from .workflows import offer_workflows
        offered = offer_workflows(chat_id, _telegram_pending_character[chat_id], character_id)
        if offered is not None:
            return offered
        job = _create_job_from_telegram(pending, brand_id, character_id)
        _telegram_pending_character.pop(chat_id, None)
        _queue_storyboard(job, chat_id)
        TelegramAdapter().send_message(chat_id, f"JOB {job.job_id}\nSTATUS: {job.status.value}\nПерсонажів не знайдено, використовується {character_id}. Генерую розкадровку…")
        return job.model_dump(mode="json")
    keyboard = {"inline_keyboard": [
        [{"text": f"👤 {char['name']}", "callback_data": f"select_character:{char['id']}"}] for char in characters
    ]}
    TelegramAdapter().send_message(chat_id, "Оберіть персонажа для цього завдання:", keyboard)
    return {"status": "character_selection", "brand_id": brand_id, "characters": [c["id"] for c in characters]}


def _handle_approve_job(callback: dict, chat_id: str, job_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    if job.approval_status == "approved":
        return TelegramAdapter().answer_callback(callback_id, f"{job_id} вже схвалено.")
    job.approval_status = "approved"
    job.status = JobStatus.READY
    store.update(job, JobStatus.READY, "APPROVED via Telegram")
    channels = store.repository.list_channels(job.brand_id)
    active_channels = [ch for ch in channels if ch.enabled]
    if not active_channels:
        TelegramAdapter().send_message(
            chat_id, f"⚠️ {job_id} схвалено, але для бренду {job.brand_id} немає активних каналів.")
        return TelegramAdapter().answer_callback(callback_id, f"{job_id} схвалено (каналів немає)")
    keyboard = [[{"text": f"📤 {ch.channel_type}: {ch.target}", "callback_data": f"publish_channel:{ch.channel_id}:{job_id}"}] for ch in active_channels]
    keyboard.append([{"text": "📢 Опублікувати всюди", "callback_data": f"publish_all:{job_id}"}])
    for admin_chat in _admin_chat_ids():
        try:
            TelegramAdapter().send_message(
                admin_chat, f"✅ {job_id} схвалено. Оберіть канали для публікації:",
                {"inline_keyboard": keyboard})
        except Exception:
            pass
    return TelegramAdapter().answer_callback(callback_id, f"{job_id} схвалено")


def _handle_reject_job(callback: dict, chat_id: str, job_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    job.approval_status = "rejected"
    store.update(job, JobStatus.CANCELLED, "REJECTED via Telegram")
    return TelegramAdapter().answer_callback(callback_id, f"{job_id} відхилено")


def _handle_publish_channel(callback: dict, chat_id: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    channel_id, _, job_id = payload.partition(":")
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    channel = store.repository.get_channel(channel_id)
    if not channel:
        return TelegramAdapter().answer_callback(callback_id, "Канал не знайдено")
    result = _publish_to_channel(job, channel)
    if result.get("status") == "PUBLISHED":
        status_text = "✅ Опубліковано"
    elif result.get("status") == "QUEUED":
        status_text = "📤 Опубліковано в чергу"
    else:
        status_text = f"❌ Помилка: {result.get('error', 'невідомо')}"
    return TelegramAdapter().answer_callback(
        callback_id, f"{channel.channel_type} ({channel.target}): {status_text}")


def _handle_publish_all(callback: dict, chat_id: str, job_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    channels = [ch for ch in store.repository.list_channels(job.brand_id) if ch.enabled]
    results = []
    for channel in channels:
        result = _publish_to_channel(job, channel)
        status = "✅" if result.get("status") == "PUBLISHED" else "❌"
        results.append(f"{status} {channel.channel_type}: {channel.target}")
    summary = "\n".join(results) or "Немає активних каналів"
    for admin_chat in _admin_chat_ids():
        try:
            TelegramAdapter().send_message(
                admin_chat, f"📊 Підсумок публікації {job_id}:\n{summary}")
        except Exception:
            pass
    return TelegramAdapter().answer_callback(callback_id, "Публікацію завершено")


def _publish_to_channel(job, channel) -> dict:
    if job.approval_status != "approved":
        return {"status": "REJECTED", "error": "Job не схвалено для публікації"}
    if not _has_publisher_worker(store):
        return {"status": "NOT_CONFIGURED", "error": "Publisher Worker не зареєстровано; публікація неможлива без Publisher Worker"}
    receipt = (getattr(job, "publication_results", None) or {}).get(channel.channel_type)
    if receipt and receipt.get("status") == "PUBLISHED":
        return {"status": "PUBLISHED", "channel": channel.channel_type,
                "remote_id": receipt.get("remote_id"), "url": receipt.get("url"),
                "duplicate": True}
    in_flight = {value for value in (getattr(job, "publish_task_ids", None) or {}).values()}
    if channel.channel_type in in_flight:
        return {"status": "QUEUED", "channel": channel.channel_type, "duplicate": True}
    queued = _enqueue_publish_task(store, job, channel.channel_type)
    if queued is None:
        return {"status": "NOT_CONFIGURED", "error": f"Publisher для {channel.channel_type} не налаштовано"}
    if job.status != JobStatus.PUBLISHING:
        transition_stage(job, StageName.PUBLISH, StageStatus.RUNNING)
        store.update(job, JobStatus.PUBLISHING,
                                  f"PUBLISHING {channel.channel_type} via Publisher Worker")
    return {"status": "QUEUED", "task_id": queued["task_id"], "channel": channel.channel_type}


def _send_approval_request(job) -> None:
    text = (
        f"📋 Новий Job потребує затвердження\n"
        f"ID: {job.job_id}\n"
        f"Бренд: {job.brand_id}\n"
        f"Тема: {job.topic[:200]}\n"
        f"Джерело: {job.source}"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "✅ Схвалити", "callback_data": f"approve:{job.job_id}"},
         {"text": "❌ Відхилити", "callback_data": f"reject:{job.job_id}"}]
    ]}
    for admin_chat in _admin_chat_ids():
        try:
            TelegramAdapter().send_message(admin_chat, text, keyboard)
        except Exception as error:
            logger.warning("Failed to send approval to %s: %s", admin_chat, error)


def _save_telegram_attachments(job, message: dict, root: Path) -> None:
    attachments = {key: message.get(key) for key in ("photo", "video", "document", "audio") if message.get(key)}
    if attachments:
        reference = root / job.job_id / "references" / "telegram.json"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text(json.dumps(attachments, ensure_ascii=False, indent=2), encoding="utf-8")
        register_artifact(job, root, reference, "input", workflow="telegram:webhook")
        store.event(job, "TELEGRAM REFERENCES RECORDED")
    adapter = TelegramAdapter()
    file_ids = []
    if message.get("photo"):
        file_ids.append(message["photo"][-1].get("file_id"))
    for key in ("video", "document", "audio"):
        if message.get(key):
            file_ids.append(message[key].get("file_id"))
    if adapter.configured():
        for file_id in filter(None, file_ids):
            try:
                data, filename = adapter.download_file(file_id, int(os.getenv("TELEGRAM_MAX_FILE_BYTES", "26214400")))
                downloaded = root / job.job_id / "references" / Path(filename).name
                if downloaded.exists():
                    downloaded = downloaded.with_name(f"{downloaded.stem}-{hashlib.sha256(data).hexdigest()[:8]}{downloaded.suffix}")
                downloaded.write_bytes(data)
                register_artifact(job, root, downloaded, "input", workflow="telegram:download")
            except Exception as error:
                store.event(job, f"TELEGRAM DOWNLOAD FAILED: {error}")


def _create_job_from_telegram(pending: dict, brand_id: str, character_id: str | None = None):
    text = pending["text"]
    source_id = pending["source_id"]
    message = pending["message"]
    chat_id = message.get("chat", {}).get("id", "unknown")
    if character_id is None:
        character_id = os.getenv("TELEGRAM_DEFAULT_CHARACTER", "did_samogon")
    source = f"telegram:{chat_id}:{source_id or 'unknown'}"
    if source_id and store.repository.has_telegram_update(chat_id, source_id):
        existing = next((job for job in store.jobs.values() if job.source == source), None)
        if existing:
            return existing
    for existing in store.jobs.values():
        if existing.source == source:
            return existing
    try:
        character = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
    except Exception as error:
        raise HTTPException(400, f"Unknown character: {character_id}") from error
    generation = character.generation if hasattr(character, 'generation') else {}
    workflow = generation.get("workflow") if isinstance(generation, dict) else getattr(character, 'workflow', None)
    job = store.create(text, character_id, int(os.getenv("TELEGRAM_DEFAULT_PRIORITY", "5")), source, workflow=workflow)
    job.brand_id = brand_id
    job.status = JobStatus.STORYBOARD_QUEUED
    job.approval_status = "pending"
    store.update(job, JobStatus.STORYBOARD_QUEUED, f"STORYBOARD QUEUED for brand {brand_id or 'default'}")
    if source_id:
        store.repository.record_telegram_update(chat_id, source_id, message)
    if pending.get("attachments"):
        _save_telegram_attachments(job, message, store.root)
    return job


def _handle_storyboard_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    raw = payload
    job_id, separator, rest = raw.partition(":")
    if not separator:
        return TelegramAdapter().answer_callback(callback_id, "Некоректна версія розкадровки")
    parts = rest.split(":")
    if not parts[0].isdigit():
        return TelegramAdapter().answer_callback(callback_id, "Некоректна версія розкадровки")
    version = int(parts[0])
    image_version = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    scene_index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    from core.storyboard import StoryboardService
    service = StoryboardService(store)
    try:
        if action == "sb_ok":
            job = service.approve(job_id, version, f"telegram:{chat_id}", expected_image_version=image_version)
            executor.submit(_prepare_and_dispatch, job)
            text = f"✅ Розкадровку {version} схвалено. Pipeline продовжено."
        elif action == "sb_reject":
            service.reject(job_id, version, f"telegram:{chat_id}")
            text = f"❌ Розкадровку {version} відхилено."
        elif action == "sb_edit":
            job = store.jobs.get(job_id)
            if not job or job.active_storyboard_version != version:
                from core.storyboard import StoryboardConflict
                raise StoryboardConflict("Версія розкадровки вже неактуальна")
            if job.storyboard_revision_chat_id and job.storyboard_revision_version:
                from core.storyboard import StoryboardConflict
                raise StoryboardConflict("Правки до розкадровки вже очікують на повідомлення")
            job.storyboard_revision_chat_id = chat_id
            job.storyboard_revision_version = version
            job.storyboard_image_revision_pending = False
            store.event(job, f"STORYBOARD {version} AWAITS REVISION TEXT")
            text = "Надішліть одним повідомленням, що потрібно змінити."
        elif action == "sb_img_ok":
            job = service.approve_images(job_id, version, f"telegram:{chat_id}", expected_image_version=image_version)
            executor.submit(_prepare_and_dispatch, job)
            text = f"🖼️ Превʼю розкадровки {version} схвалено. Відео розблоковано."
        elif action == "sb_img_regen":
            service.request_image_revision(job_id, version, f"telegram:{chat_id}", None, None, expected_image_version=image_version)
            text = "🔁 Перегенеровую всі превʼю розкадровки."
            job = store.jobs.get(job_id)
            if job:
                sb = next((s for s in job.storyboards if s.version == version), None)
                if sb and all(s.image_artifact_id for s in sb.scenes):
                    sb.image_status = "ready"
        elif action == "sb_img_edit":
            job = store.jobs.get(job_id)
            if not job or job.active_storyboard_version != version:
                from core.storyboard import StoryboardConflict
                raise StoryboardConflict("Версія розкадровки вже неактуальна")
            if job.storyboard_revision_chat_id and job.storyboard_image_revision_pending:
                from core.storyboard import StoryboardConflict
                raise StoryboardConflict("Правки до превʼю вже очікують на повідомлення")
            job.storyboard_revision_chat_id = chat_id
            job.storyboard_revision_version = version
            job.storyboard_image_revision_pending = True
            store.event(job, f"IMAGE STORYBOARD {version} AWAITS REVISION TEXT")
            text = "Надішліть правки до превʼю одним повідомленням."
        elif action == "sb_img_scene":
            service.request_image_revision(job_id, version, f"telegram:{chat_id}", [scene_index] if scene_index else None, None, expected_image_version=image_version)
            text = f"🔁 Перегенеровую превʼю сцени {scene_index or ''}."
        else:
            executor.submit(_regenerate_storyboard_and_notify, job_id, version, chat_id)
            text = "🔄 Генерую нову версію розкадровки."
        return TelegramAdapter().answer_callback(callback_id, text)
    except KeyError:
        return TelegramAdapter().answer_callback(callback_id, "Job або розкадровку не знайдено")
    except Exception as error:
        from core.storyboard import StoryboardConflict
        if isinstance(error, StoryboardConflict):
            return TelegramAdapter().answer_callback(callback_id, str(error))
        raise


def _handle_script_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job_id = payload
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job не знайдено")
    from core.pipeline import approve_script, request_script_revision, regenerate_script
    from core.storyboard_telegram import render_script, script_keyboard
    try:
        if action == "sc_ok":
            job = approve_script(store, job, f"telegram:{chat_id}")
            executor.submit(_prepare_and_dispatch, job)
            text = f"✅ Сценарій {job_id} схвалено."
        elif action == "sc_edit":
            if job.status not in {JobStatus.SCRIPT_PENDING_APPROVAL, JobStatus.SCRIPT_REVISION_REQUESTED}:
                raise ValueError(f"Сценарій не очікує правок (status={job.status.value})")
            if job.script_revision_pending and job.script_revision_chat_id:
                raise ValueError("Правки до сценарію вже очікують на повідомлення")
            job.script_revision_chat_id = chat_id
            job.script_revision_pending = True
            store.event(job, f"SCRIPT AWAITS REVISION TEXT")
            text = "Надішліть одним повідомленням, що потрібно змінити у сценарії."
        elif action == "sc_regen":
            if job.status == JobStatus.SCRIPT_REVISION_REQUESTED:
                job = regenerate_script(store, job)
                executor.submit(_prepare_and_dispatch, job)
                text = "🔄 Перегенеровую сценарій…"
            elif job.status == JobStatus.SCRIPT_PENDING_APPROVAL:
                job.script = None
                job.version += 1
                store.transition(job, JobStatus.SCRIPT_REVISION_REQUESTED, f"SCRIPT REGENERATION REQUESTED by telegram:{chat_id}")
                executor.submit(_prepare_and_dispatch, job)
                text = "🔄 Перегенеровую сценарій…"
            else:
                text = f"Неможливо перегенерувати (status={job.status.value})"
        elif action == "sc_reject":
            store.update(job, JobStatus.CANCELLED, "SCRIPT REJECTED via Telegram")
            text = f"❌ Сценарій {job_id} відхилено."
        else:
            text = f"Невідома дія: {action}"
        return TelegramAdapter().answer_callback(callback_id, text)
    except ValueError as error:
        return TelegramAdapter().answer_callback(callback_id, str(error))


def _handle_video_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    parts = payload.split(":", 1)
    job_id = parts[0]
    expected_version = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job не знайдено")
    source_parts = str(job.source or "").split(":")
    source_chat_id = source_parts[1] if len(source_parts) >= 2 and source_parts[0] == "telegram" else None
    if source_chat_id is not None and source_chat_id != chat_id:
        if action in {"vid_ok", "vid_reject", "vid_edit", "vid_regen"}:
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: chat не є власником job")
    if not is_admin_chat(chat_id) and source_chat_id != chat_id:
        return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: лише для адміністраторів.")
    from core.pipeline import approve_video, claim_video_regeneration, request_video_revision
    try:
        if action == "vid_ok":
            job = approve_video(store, job, f"telegram:{chat_id}", expected_version=expected_version)
            text = f"✅ Відео {job_id} схвалено."
        elif action == "vid_edit":
            if job.status != JobStatus.VIDEO_PENDING_APPROVAL:
                raise ValueError(f"Відео не очікує правок (status={job.status.value})")
            if job.video_revision_pending and job.video_revision_chat_id:
                raise ValueError("Правки до відео вже очікують на повідомлення")
            job.video_revision_chat_id = chat_id
            job.video_revision_pending = True
            store.event(job, "VIDEO AWAITS REVISION TEXT")
            text = "Надішліть одним повідомленням, що потрібно змінити у відео."
        elif action == "vid_regen":
            if job.video_regenerating:
                return TelegramAdapter().answer_callback(callback_id, "🔄 Відео вже генерується")
            if job.status == JobStatus.VIDEO_PENDING_APPROVAL:
                job = request_video_revision(store, job, "regenerate", f"telegram:{chat_id}")
            elif job.status != JobStatus.VIDEO_REVISION_REQUESTED:
                text = f"Неможливо перегенерувати (status={job.status.value})"
                return TelegramAdapter().answer_callback(callback_id, text)
            try:
                claim_video_regeneration(store, job, f"telegram:{chat_id}")
            except ValueError:
                return TelegramAdapter().answer_callback(callback_id, "🔄 Відео вже генерується")
            executor.submit(_finalize_video_regenerate, job)
            text = "🔄 Перегенеровую відео…"
        elif action == "vid_reject":
            if expected_version is not None and expected_version != job.active_video_version:
                raise ValueError(f"Stale video rejection: expected v{expected_version}, active is v{job.active_video_version}")
            store.update(job, JobStatus.CANCELLED, "VIDEO REJECTED via Telegram")
            text = f"❌ Відео {job_id} відхилено."
        else:
            text = f"Невідома дія: {action}"
        return TelegramAdapter().answer_callback(callback_id, text)
    except ValueError as error:
        return TelegramAdapter().answer_callback(callback_id, str(error))








def _queue_storyboard(job, chat_id: str, revision: str | None = None) -> None:
    from core.storyboard import StoryboardService
    service = StoryboardService(store)
    service.store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
    executor.submit(_generate_storyboard_and_notify, job.job_id, chat_id, revision)


def _generate_storyboard_and_notify(job_id: str, chat_id: str, revision: str | None = None) -> None:
    from core.storyboard import StoryboardService
    try:
        StoryboardService(store).generate(job_id, revision)
    except Exception as error:
        logger.error("Storyboard generation failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Не вдалося створити розкадровку {job_id}: {error}")
        except Exception:
            pass


def _regenerate_script_and_notify(job_id: str, chat_id: str, revision: str | None = None) -> None:
    """Regenerate script after revision request from Telegram."""
    try:
        from core.pipeline import regenerate_script
        job = store.jobs.get(job_id)
        if not job:
            return
        regenerate_script(store, job)
    except Exception as error:
        logger.error("Script regeneration failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Помилка перегенерації сценарію {job_id}: {error}")
        except Exception:
            pass


def _regenerate_storyboard_and_notify(job_id: str, version: int, chat_id: str,
                                      revision: str | None = None) -> None:
    from core.storyboard import StoryboardService
    try:
        StoryboardService(store).regenerate(
            job_id, version, f"telegram:{chat_id}", revision
        )
    except Exception as error:
        logger.error("Storyboard regeneration failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Не вдалося оновити розкадровку {job_id}: {error}")
        except Exception:
            pass
def _handle_character_selection(callback: dict, chat_id: str, character_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    pending_data = _telegram_pending_character.get(chat_id)
    if not pending_data:
        return TelegramAdapter().answer_callback(callback_id, "Сесію закрито. Почніть спочатку.")
    pending = pending_data["pending"]
    brand_id = pending_data["brand_id"]
    adapter = TelegramAdapter()
    try:
        adapter.answer_callback(callback_id, "Перевіряю персонажа…")
    except Exception:
        logger.warning("Telegram callback acknowledgement failed")
    try:
        load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
        from .workflows import offer_workflows
        offered = offer_workflows(chat_id, pending_data, character_id)
        if offered is not None:
            return offered
        job = _create_job_from_telegram(pending, brand_id, character_id)
    except Exception as error:
        logger.exception("Telegram Job creation failed", extra={"character_id": character_id})
        error_detail = _sanitize_creation_error(error)
        adapter.send_message(chat_id, f"❌ Не вдалося завершити створення завдання: {error_detail}\nПеревірте конфігурацію персонажа та сховище. Вибір збережено — можна повторити спробу.")
        return {"status": "creation_failed", "error": error_detail}
    _telegram_pending_character.pop(chat_id, None)
    try:
        adapter.send_message(chat_id, f"✅ Завдання {job.job_id} збережено. Воно доступне в адмінці у списку «Завдання».")
    except Exception:
        logger.warning("Telegram Job confirmation failed", extra={"job_id": job.job_id})
    try:
        _queue_storyboard(job, chat_id)
    except Exception:
        logger.exception("Telegram storyboard queue failed", extra={"job_id": job.job_id})
        adapter.send_message(chat_id, f"❌ Завдання {job.job_id} збережено, але обробку не вдалося запустити. Перевірте його стан в адмінці.")
    return job.model_dump(mode="json")


_PROCESS_STARTED_AT = datetime.now(timezone.utc)


_telegram_system_callbacks = DurableCallbackSet()


_telegram_system_restore: dict[str, dict] = {}


_ACTION_TO_OP_TYPE = {
    "sys_update": "update", "sys_update_check": "update", "sys_update_confirm": "update",
    "sys_restart": "restart", "sys_restart_core": "restart", "sys_restart_worker": "restart",
    "sys_backup": "backup", "sys_backup_confirm": "backup",
    "sys_restore": "restore", "sys_restore_confirm": "restore", "sys_restore_execute": "restore",
    "sys_test": "test", "sys_test_quick": "test", "sys_test_full": "test", "sys_test_node": "test",
}


_ABS_PATH_RE = re.compile(r"(?<![:/])(?<!\w)(?:[A-Za-z]:[\\/]|/)[^\s'\";)]*")


def _admin_chat_ids() -> list[str]:
    return get_admin_chat_ids()


def _is_admin_chat(chat_id: str) -> bool:
    return is_admin_chat(chat_id)




def _host_status_is_stale(status: dict) -> bool:
    """True when the durable update status stopped progressing.

    The host agent refreshes ``status.json`` while it works on a request.  A
    status that stayed in ``PENDING``/``RUNNING`` longer than the configured
    window belongs to a host agent that died mid-operation (Issue #174.3).
    """
    try:
        window = int(os.getenv("UPDATE_OPERATION_STALE_SECONDS", "3600"))
    except ValueError:
        window = 3600
    if window <= 0:
        return False
    updated_at = str(status.get("updated_at") or "")
    if not updated_at:
        return False
    try:
        moment = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() > window


def _operation_started_before_process(operation: dict) -> bool:
    """True when the operation was created by an earlier CORE process."""
    created_at = str(operation.get("created_at") or "")
    if not created_at:
        return False
    try:
        moment = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment < _PROCESS_STARTED_AT








def _link_callback_to_operation(callback_id: str, action: str, payload: str,
                                chat_id: str) -> None:
    """Link a CLAIMED callback to its terminal operation for crash-replay checking.

    After the side effect runs, its operation may have reached COMPLETED/FAILED.
    Linking the callback record to that operation lets a later replay detect the
    terminal state and skip re-execution (effect→crash→persist gap, Issue #174).
    """
    op_type = _ACTION_TO_OP_TYPE.get(action)
    if not op_type:
        return
    try:
        operations = list_operations(200)
    except Exception:
        return
    target = payload or None
    requested_by = f"telegram:{chat_id}"
    terminal = {"COMPLETED", "FAILED", "CANCELLED"}
    best = None
    for op in operations:
        if (op.get("type") == op_type
                and op.get("target") == target
                and op.get("requested_by") == requested_by
                and op.get("status") in terminal):
            finished = op.get("finished_at") or ""
            if best is None or finished > best.get("finished_at", ""):
                best = op
    if best and best.get("operation_id"):
        link_operation(callback_id, best["operation_id"])






def _call_core_api(method: str, path: str, payload: dict | None = None,
                   timeout: float = 30.0) -> dict | None:
    """Call the CORE API directly (synchronous).

    Passes the ``INTERNAL_API_KEY`` so that admin-only endpoints invoked by
    Telegram handlers (restart, test, backups, node actions) authenticate
    correctly without requiring user credentials.
    """
    return _sync_internal_api(method, "CORE_API_URL", path, payload, timeout=timeout,
                              headers=_internal_auth_headers())


def _call_backup_api(method: str, path: str, payload: dict | None = None,
                     timeout: float = 120.0) -> dict | None:
    """Call the Backup Service directly (synchronous)."""
    return _sync_internal_api(method, "BACKUP_URL", path, payload, timeout=timeout)
