"""Telegram system status, formatting and reconciliation of update/restart operations."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import httpx

from adapters.telegram import TelegramAdapter
from core.api._internal import _sync_internal_api
from core.operations import (
    OperationStatus,
    pending_notifications,
    acknowledge_notification,
    add_pending_notification,
    advance_operation,
    complete_operation,
    fail_operation,
    list_operations,
)
from core.runtime_identity import CORE_RUNTIME_INSTANCE_ID
from core.state import (
    logger,
    store,
)
from core.system_state import get_system_state
from core.update_manager import (
    pending_request_ids,
    mark_request_failed,
    update_status,
)

from .common import (
    _queue_operation_notification,
    _host_status_is_stale,
    _operation_started_before_process,
    _call_core_api,
)


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

    dispatcher_url = os.getenv("DISPATCHER_URL", "").rstrip("/")
    # Without a separate Dispatcher service it runs inside CORE: report whether it
    # actually hands out tasks now instead of an unchecked "running".
    from core.system_state import dispatch_allowed
    dispatcher_ok = "running" if dispatch_allowed() else "paused"
    if dispatcher_url:
        dispatcher_ok = "running"
        try:
            httpx.get(f"{dispatcher_url}/health", timeout=3).raise_for_status()
        except httpx.HTTPError:
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
    nodes = _call_core_api("GET", "/api/nodes")
    if not isinstance(nodes, list):
        return "Вузли: дані недоступні"
    online = sum(1 for n in nodes if (n.get("runtime_status") or n.get("status")) == "ONLINE")
    return f"Вузли: {online}/{len(nodes)} online"


def _format_jobs_summary() -> str:
    # Telegram runs inside CORE: the store is the source, no self-call is needed.
    jobs = [{"status": job.status.value} for job in store.jobs.values()]
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
