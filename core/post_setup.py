"""What an administrator must still do by hand after an install or an update."""

import json
import os

from core.first_run import config_root, ensure_secret_store
from core.telegram_store import get_admin_chat_ids, get_allowed_chat_ids

_ACK_FILE = "post-setup.json"
_KEY_FILE = "backup.key"


def _key_acknowledged() -> bool:
    try:
        return bool(json.loads((config_root() / _ACK_FILE).read_text(encoding="utf-8")).get("backup_key_saved"))
    except (OSError, ValueError, AttributeError):
        return False


def post_setup_state() -> dict:
    token = ensure_secret_store().get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN")
    telegram = bool(token) and not get_allowed_chat_ids() and not get_admin_chat_ids()
    backup_key = (config_root() / _KEY_FILE).is_file() and not _key_acknowledged()
    return {"pending": telegram or backup_key, "telegram": telegram, "backup_key": backup_key}


def read_backup_key() -> str | None:
    try:
        return (config_root() / _KEY_FILE).read_text(encoding="utf-8")
    except OSError:
        return None


def acknowledge_backup_key() -> dict:
    root = config_root()
    temporary = root / f".{_ACK_FILE}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps({"backup_key_saved": True}), encoding="utf-8")
    temporary.replace(root / _ACK_FILE)
    return post_setup_state()
