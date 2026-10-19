"""Neutral dispatch point for Telegram polling control (Issue #110 Phase B).

``core/app.py`` registers the real start/stop/restart functions at import time.
API modules call them through this registry instead of importing ``core.app``,
which would create a circular dependency between ``core/api/*`` and the
composition root.
"""

_restart_handler = None
_service_getter = None


def register_restart(handler) -> None:
    """Register the real ``_restart_telegram_polling`` implementation."""
    global _restart_handler
    _restart_handler = handler


def restart_polling() -> None:
    """Restart Telegram polling via the registered handler, if any."""
    if _restart_handler is not None:
        _restart_handler()


def register_service(getter) -> None:
    """Register a callable returning the live polling service (or None)."""
    global _service_getter
    _service_getter = getter


def service_snapshot() -> dict:
    """Return polling status fields without importing ``core.app``."""
    service = _service_getter() if _service_getter is not None else None
    if service is None:
        return {"polling_status": "stopped", "bot_username": None,
                "last_update_id": None, "last_message_at": None}
    from adapters.telegram import TelegramAdapter
    bot_username = None
    try:
        adapter = TelegramAdapter()
        if adapter.configured:
            me = adapter.get_me()
            if me.get("ok"):
                bot_username = me.get("result", {}).get("username")
    except Exception:
        bot_username = None
    return {"polling_status": "running" if service.running else "stopped",
            "bot_username": bot_username,
            "last_update_id": service.last_update_id,
            "last_message_at": service.last_message_at}