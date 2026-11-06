"""Publication parameters the owner enters in Settings → Публікація."""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

from core.first_run import config_root

_FILE = "publishing-settings.json"


def publishing_settings() -> dict:
    try:
        stored = json.loads((config_root() / _FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    return {"public_url": str(stored.get("public_url") or ""),
            "telegram_channel_id": str(stored.get("telegram_channel_id") or "")}


def save_publishing_settings(public_url: str, telegram_channel_id: str) -> dict:
    public_url = public_url.strip().rstrip("/")
    telegram_channel_id = telegram_channel_id.strip()
    if public_url:
        parts = urlsplit(public_url)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
            raise ValueError("Публічна адреса має мати вигляд https://домен")
    if telegram_channel_id and not re.fullmatch(r"@[A-Za-z0-9_]{5,32}|-?\d{1,20}", telegram_channel_id):
        raise ValueError("Канал Telegram: @назва або числовий ідентифікатор")
    value = {"public_url": public_url, "telegram_channel_id": telegram_channel_id}
    path = config_root() / _FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{_FILE}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return value
