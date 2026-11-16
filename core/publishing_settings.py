"""Publication parameters the owner enters in Settings → Публікація.

var.0.0.4.86: the Telegram channel is a target of a brand (Бренди → Канали), not
an installation-wide setting; a former ``telegram_channel_id`` is not migrated
(owner decision) and is ignored.
"""

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
    return {"public_url": str(stored.get("public_url") or "")}


TELEGRAM_CHANNEL = re.compile(r"@[A-Za-z0-9_]{5,32}|-?\d{1,20}")


def valid_telegram_channel(target: str) -> bool:
    """A Telegram channel target: @username or a numeric chat id."""
    return bool(TELEGRAM_CHANNEL.fullmatch(target.strip()))


def save_publishing_settings(public_url: str) -> dict:
    public_url = public_url.strip().rstrip("/")
    if public_url:
        parts = urlsplit(public_url)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
            raise ValueError("Публічна адреса має мати вигляд https://домен")
    value = {"public_url": public_url}
    path = config_root() / _FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{_FILE}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return value
