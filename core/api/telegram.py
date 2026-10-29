from __future__ import annotations

from adapters.telegram import TelegramAdapter
from core.models import TelegramSetup
from core.telegram_store import get_admin_chat_ids
from core.telegram_store import get_allowed_chat_ids
from core.telegram_store import load_telegram_settings
from core.telegram_store import save_telegram_settings
from fastapi import HTTPException
from fastapi import Request
import httpx
import os
import secrets
from fastapi import APIRouter
router = APIRouter()
from core.telegram import handlers
from core.telegram.handlers import _handle_telegram_callback, _handle_telegram_message, _restart_telegram_polling

@router.post("/api/telegram/webhook")
def telegram_webhook(update: dict, request: Request):
    if os.getenv("TELEGRAM_WEBHOOK_ENABLED", "").lower() not in {"1", "true", "yes"}:
        raise HTTPException(404, "Telegram webhook is disabled; use polling as the primary integration")
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if webhook_secret and not secrets.compare_digest(request.headers.get("x-telegram-bot-api-secret-token", ""), webhook_secret):
        raise HTTPException(401, "Invalid Telegram webhook secret")
    callback = update.get("callback_query")
    if callback:
        return _handle_telegram_callback(callback)
    message = update.get("message", {})
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        raise HTTPException(400, "Telegram update has no text")
    chat_id = str(message.get("chat", {}).get("id", "unknown"))
    allowed = get_allowed_chat_ids()
    admin_ids = set(get_admin_chat_ids())
    if allowed and chat_id not in allowed and chat_id not in admin_ids:
        raise HTTPException(403, "Telegram chat is not allowed")
    source_id = str(message.get("message_id", ""))
    return _handle_telegram_message(chat_id, source_id, text, message)


@router.post("/api/telegram/setup")
def telegram_setup(body: TelegramSetup):
    from core.first_run import ensure_secret_store
    secrets = ensure_secret_store()
    token = secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise HTTPException(400, "TELEGRAM_BOT_TOKEN is not configured")
    webhook_secret = body.webhook_secret or os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if webhook_secret:
        os.environ["TELEGRAM_WEBHOOK_SECRET"] = webhook_secret
    save_telegram_settings(allowed_chat_ids=body.allowed_chat_ids, admin_chat_ids=body.admin_chat_ids)
    return {"status": "saved", "message": "Telegram polling configuration saved; webhook is legacy"}


@router.get("/api/telegram/status")
def telegram_status():
    from core.first_run import ensure_secret_store
    secrets = ensure_secret_store()
    token_configured = bool(secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    public_url = os.getenv("PUBLIC_URL", "")
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    settings = load_telegram_settings()
    allowed_chat_ids = settings.get("allowed_chat_ids", "")
    admin_chat_ids = settings.get("admin_chat_ids", "")
    polling_enabled = os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() == "true"
    polling_status = "stopped"
    bot_username = None
    last_update_id = None
    last_message_at = None
    if handlers.telegram_polling_service is not None:
        polling_status = "running" if handlers.telegram_polling_service.running else "stopped"
        bot_username = _get_bot_username()
        last_update_id = handlers.telegram_polling_service.last_update_id
        last_message_at = handlers.telegram_polling_service.last_message_at
    return {
        "configured": token_configured,
        "webhook_url": f"{public_url.rstrip('/')}/api/telegram/webhook" if public_url else "",
        "public_url": public_url,
        "webhook_secret_configured": bool(webhook_secret),
        "allowed_chat_ids": allowed_chat_ids,
        "admin_chat_ids": admin_chat_ids,
        "polling_enabled": polling_enabled,
        "polling_status": polling_status,
        "bot_username": bot_username,
        "last_update_id": last_update_id,
        "last_message_at": last_message_at,
    }


def _get_bot_username() -> str | None:
    try:
        adapter = TelegramAdapter()
        if not adapter.configured():
            return None
        result = adapter.get_me()
        if result.get("ok"):
            return result.get("result", {}).get("username")
    except Exception:
        pass
    return None


@router.get("/api/telegram/bot-info")
def telegram_bot_info():
    from core.first_run import ensure_secret_store
    secrets = ensure_secret_store()
    token_configured = bool(secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    if not token_configured:
        return {"configured": False}
    adapter = TelegramAdapter()
    try:
        me = adapter.get_me()
        if not me.get("ok"):
            return {"configured": True, "ok": False, "error": me.get("description")}
        return {
            "configured": True,
            "ok": True,
            "bot_username": me.get("result", {}).get("username"),
            "bot_id": me.get("result", {}).get("id"),
        }
    except (httpx.HTTPError, RuntimeError) as error:
        return {"configured": True, "ok": False, "error": str(error)}

