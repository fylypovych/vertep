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


@pytest.mark.parametrize("channels,ready", [([], False), (["@vertep_channel"], True)])
def test_telegram_publication_is_ready_only_with_a_channel(monkeypatch, channels, ready):
    """The publisher-bot token alone is not a publication target: the tile must not say «Готово».
    var.0.0.4.86: the target is a brand's Telegram channel."""
    from adapters.publisher import PUBLISHERS
    import core.publishing_targets as targets
    monkeypatch.delenv("PUBLISHER_MOCK", raising=False)
    monkeypatch.setenv("TELEGRAM_PUBLISHER_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(targets, "all_targets", lambda channel_type: list(channels))
    assert PUBLISHERS["telegram"].ready() is ready


def test_the_request_bot_token_does_not_make_publication_ready(monkeypatch):
    """var.0.0.4.86: only the separate publisher bot publishes to the channel."""
    from adapters.publisher import PUBLISHERS
    import core.publishing_targets as targets
    monkeypatch.delenv("PUBLISHER_MOCK", raising=False)
    monkeypatch.delenv("TELEGRAM_PUBLISHER_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(targets, "all_targets", lambda channel_type: ["@vertep_channel"])
    monkeypatch.setattr("publishers.oauth._get_secret",
                        lambda name: "123:abc" if name.upper() == "TELEGRAM_BOT_TOKEN" else None)
    assert PUBLISHERS["telegram"].ready() is False
