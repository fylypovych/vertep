"""Постійне сховище налаштувань Telegram."""

import json
import os
import threading
from pathlib import Path

from .first_run import config_root

_lock = threading.RLock()
_SETTINGS_FILE = "telegram-settings.json"


def _path() -> Path:
    return config_root() / _SETTINGS_FILE


def load_telegram_settings() -> dict:
    """Завантажує налаштування Telegram з постійного сховища.

    Якщо сховище недоступне, повертає значення зі змінних оточення (backward compat).
    """
    try:
        with _lock:
            data = json.loads(_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {
        "allowed_chat_ids": os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""),
        "admin_chat_ids": os.getenv("TELEGRAM_ADMIN_CHAT_IDS", ""),
    }


def save_telegram_settings(allowed_chat_ids: str | None = None,
                           admin_chat_ids: str | None = None) -> dict:
    """Зберігає налаштування Telegram у постійне сховище.

    Повертає оновлені налаштування.
    """
    with _lock:
        current = load_telegram_settings()
        if allowed_chat_ids is not None:
            current["allowed_chat_ids"] = allowed_chat_ids
        if admin_chat_ids is not None:
            current["admin_chat_ids"] = admin_chat_ids
        root = config_root()
        root.mkdir(parents=True, exist_ok=True)
        path = _path()
        temporary = root / f".{path.name}.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        return current


def get_allowed_chat_ids() -> set[str]:
    """Повертає множину дозволених chat ID."""
    settings = load_telegram_settings()
    raw = settings.get("allowed_chat_ids", "")
    if isinstance(raw, str):
        return {item.strip() for item in raw.split(",") if item.strip()}
    if isinstance(raw, list):
        return {str(item).strip() for item in raw if str(item).strip()}
    return set()


def get_admin_chat_ids() -> list[str]:
    """Повертає список admin chat ID."""
    settings = load_telegram_settings()
    raw = settings.get("admin_chat_ids", "")
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def is_admin_chat(chat_id: str) -> bool:
    """Перевіряє, чи є користувач admin-ом."""
    return chat_id in get_admin_chat_ids()


def is_allowed_chat(chat_id: str) -> bool:
    """Перевіряє, чи дозволений чат."""
    allowed = get_allowed_chat_ids()
    if not allowed:
        return False
    return chat_id in allowed
