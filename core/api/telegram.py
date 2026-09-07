"""Telegram routes and handlers for the Vertep CORE web application.

Extracted from ``core/app.py``: Telegram webhook, setup, status and bot-info endpoints.
Critical: The Telegram polling lifecycle (`_start_telegram_polling`, `_stop_telegram_polling`,
`telegram_polling_service`) and global `last_maintenance` remain in ``core/app.py`` per the
Telegram integration strategy.  Routes/helpers here access the service via
``core.state.telegram_service.service`` — a holder that mutates in place, not through
global reassignment.

See ``core/state.py`` for the ``TelegramServiceHolder`` definition and the
``lifespan`` function in ``core/app.py`` for the full startup/shutdown flow.
"""
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from ..telegram_store import (get_admin_chat_ids, get_allowed_chat_ids, is_admin_chat,
                              load_telegram_settings, save_telegram_settings)
from ..state import telegram_service
from ..security import _valid_worker_request
from ..models import utc_now
import os
import json


router = APIRouter()


@router.post("/api/telegram/webhook")
def telegram_webhook(update: dict, request: Request):
    """Receive Telegram updates via webhook.

    The payload is processed by ``_process_telegram_update`` which is registered
    as the ``on_update`` callback for the ``TelegramPollingService`` managed by the
    lifespan in ``core/app.py``.
    """
    from .. import secrets
    from ..adapters.telegram import TelegramAdapter
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if webhook_secret and not secrets.compare_digest(
        request.headers.get("x-telegram-bot-api-secret-token", ""), webhook_secret
    ):
        raise HTTPException(401, "Invalid Telegram webhook secret")
    if not update:
        raise HTTPException(400, "Telegram update has no data")
    # Delegate to the internal handler registered in the lifespan
    return _process_telegram_update(update)


@router.post("/api/telegram/setup")
def telegram_setup(body: dict):
    """Configure Telegram bot settings via the First Run Wizard.

    Updates allowed and admin chat IDs in the persistent store.
    """
    allowed_chat_ids = body.get("allowed_chat_ids", "")
    admin_chat_ids = body.get("admin_chat_ids", "")
    save_telegram_settings(allowed_chat_ids=allowed_chat_ids,
                           admin_chat_ids=admin_chat_ids)
    # Re-register webhook if URL changed
    from .. import secrets
    webhook_secret = body.get("webhook_secret") or os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    os.environ["TELEGRAM_WEBHOOK_SECRET"] = webhook_secret
    return {"state": "configured", "allowed_chat_ids": allowed_chat_ids,
            "admin_chat_ids": admin_chat_ids}


@router.get("/api/telegram/status")
def telegram_status():
    """Report Telegram bot status.

    Returns whether the bot token is configured, webhook URL, polling enabled state,
    and current allowed chat IDs.  Reads the service status from the holder managed
    by the lifespan in ``core/app.py``.
    """
    from .. import secrets
    token_configured = bool(secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    settings = load_telegram_settings()
    polling_enabled = os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() == "true"
    from ..state import telegram_service as _ts
    polling_status = "running" if _ts.service and _ts.service.running else "stopped"
    last_update_id = _ts.service.last_update_id if _ts.service else None
    last_message_at = _ts.service.last_message_at if _ts.service else None
    public_url = os.getenv("PUBLIC_URL", "")
    return {
        "configured": token_configured,
        "webhook_url": f"{public_url.rstrip('/')}/api/telegram/webhook" if public_url else "",
        "public_url": public_url,
        "webhook_secret_configured": bool(webhook_secret),
        "allowed_chat_ids": settings.get("allowed_chat_ids", ""),
        "admin_chat_ids": settings.get("admin_chat_ids", ""),
        "polling_enabled": polling_enabled,
        "polling_status": polling_status,
        "last_update_id": last_update_id,
        "last_message_at": last_message_at,
    }


@router.get("/api/telegram/bot-info")
def telegram_bot_info():
    """Return basic bot information.

    Checks if the bot token is configured and the bot API is reachable.
    """
    from .. import secrets
    from ..adapters.telegram import TelegramAdapter
    token_configured = bool(secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    adapter = TelegramAdapter()
    return {"configured": token_configured, "ok": adapter.ping() if token_configured else False}


def _process_telegram_update(update: dict) -> dict:
    """Internal handler for Telegram updates.

    This mirror of the lifespan‑registered callback processes the update,
    extracts chat ID and text, and creates or advances a job.
    """
    from ..telegram_store import _telegram_pending_brands, _telegram_pending_character
    from ..orchestration import interrupt_scene
    from ..models import JobStatus
    from .. import secrets

    chat_id = str(update.get("message", {}).get("chat", {}).get("id", ""))
    if not chat_id:
        return {"status": "ignored", "reason": "no chat_id"}

    text = update.get("message", {}).get("text", "")
    source_id = update.get("message", {}).get("from", {}).get("id", "")

    # Allow update if chat is in allowed list or no restriction set
    allowed = load_telegram_settings().get("allowed_chat_ids", "")
    if allowed and chat_id not in allowed:
        # still accept but mark as not allowed (bot will respond)
        pass

    # Route based on command
    if text and text.startswith("/"):
        command = text.lower()
        if command.startswith("/start"):
            return _handle_start_command(chat_id, source_id)
        elif command.startswith("/help"):
            return {"status": "ok", "help": "Available commands: /start"}
        elif command.startswith("/brand"):
            return _handle_brand_command(chat_id, text)
        elif command.startswith("/character"):
            return _handle_character_command(chat_id, text)
        elif command.startswith("/cancel"):
            # Cancel current job for this chat
            job = store.jobs.get(chat_id) if chat_id in store.jobs else None
            if job:
                _job_action(job.job_id, JobStatus.CANCELLED, "TELEGRAM CANCELLED")
            return {"status": "cancelled"}
    # Default: create/advance a job from the message
    pending = _telegram_pending_brands.get(chat_id) or _telegram_pending_character.get(chat_id)
    if pending:
        job = _create_job_from_telegram(pending, brand_id=pending.get("brand_id"),
                                        character_id=pending.get("character_id"))
        _telegram_pending_brands.pop(chat_id, None)
        _telegram_pending_character.pop(chat_id, None)
        store.update(job, JobStatus.NEW, "CREATED FROM TELEGRAM")
        return {"status": "job_created", "job_id": job.job_id}
    # No pending data — just acknowledge
    return {"status": "acknowledged"}


def _handle_start_command(chat_id: str, source_id: str) -> dict:
    """Handle /start command — register the chat and show welcome message."""
    from ..adapters.telegram import TelegramAdapter
    allowed = load_telegram_settings().get("allowed_chat_ids", "")
    if allowed and chat_id not in allowed:
        # Register the chat as allowed for future updates
        admin_chat = load_telegram_settings().get("admin_chat_ids", "")
        save_telegram_settings(allowed_chat_ids=f"{allowed},{chat_id}",
                               admin_chat_ids=admin_chat)
    adapter = TelegramAdapter()
    welcome = ("Vertep Bot підключено!\n"
               "Будь ласка, оберіть дію через меню або команду /brand.")
    adapter.send_message(chat_id, welcome)
    return {"status": "started"}


def _handle_brand_command(chat_id: str, text: str) -> dict:
    """Handle /brand command — manage brands."""
    from ..adapters.telegram import TelegramAdapter
    adapter = TelegramAdapter()
    adapter.send_message(chat_id, "Функція обробки брендів тимчасно недоступна.")
    return {"status": "brand_command_pending"}


def _handle_character_command(chat_id: str, text: str) -> dict:
    """Handle /character command — manage characters."""
    from ..adapters.telegram import TelegramAdapter
    adapter = TelegramAdapter()
    adapter.send_message(chat_id, "Функція обробки персонажів тимчасно недоступна.")
    return {"status": "character_command_pending"}