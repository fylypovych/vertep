"""A bot token entered in the First Run Wizard must start Telegram polling at once."""
import hashlib

import pytest
from fastapi.testclient import TestClient

from core import post_setup, telegram_pump, telegram_store
from core.api import setup as setup_api
from core.app import app


@pytest.mark.parametrize("token,restarts", [("123:abc", 1), ("", 0)])
def test_wizard_bot_token_starts_polling(monkeypatch, tmp_path, token, restarts):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("SETUP_TOKEN_HASH", hashlib.sha256(b"setup-code").hexdigest())
    monkeypatch.delenv("SETUP_TOKEN_EXPIRES_AT", raising=False)

    async def valid(*_):
        return None
    monkeypatch.setattr(setup_api, "_validate_ai_backend", valid)
    monkeypatch.setattr(setup_api, "create_registration_token", lambda *a: "VT-token")
    monkeypatch.setattr(setup_api, "complete_setup", lambda *a: {"installation_id": "i"})
    monkeypatch.setattr(setup_api, "set_integration_secret", lambda *a: {})
    monkeypatch.setattr(telegram_store, "save_telegram_settings", lambda **k: None)
    monkeypatch.setattr(post_setup, "read_backup_key", lambda: None)
    calls = []
    monkeypatch.setattr(telegram_pump, "restart_polling", lambda: calls.append(1))

    response = TestClient(app).post("/api/setup/complete", headers={"X-Vertep-Setup-Token": "setup-code"},
                                    json={"node_role": "core", "ai_backend": "skip", "telegram_bot_token": token})
    assert response.status_code == 200, response.text
    assert len(calls) == restarts


@pytest.mark.parametrize("channel,ready", [("", False), ("@vertep_channel", True)])
def test_telegram_publication_is_ready_only_with_a_channel(monkeypatch, channel, ready):
    """The bot token alone is not a publication target: the tile must not say «Готово»."""
    from adapters.publisher import TelegramChannelPublisher
    import core.publishing_settings as settings
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(settings, "publishing_settings", lambda: {"public_url": "", "telegram_channel_id": channel})
    assert TelegramChannelPublisher().ready() is ready
