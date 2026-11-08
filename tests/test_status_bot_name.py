"""/api/status is read by every panel page: it must not wait for Telegram getMe."""
import threading
import time

import pytest

from core.api import telegram


@pytest.fixture
def bot(monkeypatch):
    telegram._bot_name.update(key=None, value=None, at=0.0, refreshing=False)
    gate, calls = threading.Event(), []

    class Adapter:
        token = "123:token"

        def configured(self):
            return True

        def get_me(self):
            calls.append(self.token)
            gate.wait(5)
            return {"ok": True, "result": {"username": f"bot_{self.token[:3]}"}}

    monkeypatch.setattr(telegram, "TelegramAdapter", Adapter)
    yield Adapter, gate, calls
    gate.set()


def _wait_for(predicate):
    end = time.monotonic() + 5
    while time.monotonic() < end and not predicate():
        time.sleep(0.01)
    assert predicate()


def test_a_slow_telegram_does_not_delay_the_status(bot):
    adapter, gate, calls = bot
    started = time.monotonic()
    assert telegram._get_bot_username() is None
    assert telegram._get_bot_username() is None
    assert time.monotonic() - started < 1, "the page waited for Telegram"
    gate.set()
    _wait_for(lambda: telegram._get_bot_username() == "bot_123")
    assert len(calls) == 1, "a refresh already running is not started again"


def test_the_name_is_reused_and_a_failure_keeps_the_last_one(bot, monkeypatch):
    adapter, gate, calls = bot
    gate.set()
    telegram._get_bot_username()
    _wait_for(lambda: telegram._get_bot_username() == "bot_123")
    assert len(calls) == 1
    monkeypatch.setattr(telegram, "_BOT_NAME_TTL_SECONDS", 0)
    monkeypatch.setattr(adapter, "get_me", lambda self: (_ for _ in ()).throw(RuntimeError("timed out")))
    assert telegram._get_bot_username() == "bot_123"
    _wait_for(lambda: not telegram._bot_name["refreshing"])
    assert telegram._get_bot_username() == "bot_123"


def test_another_bot_does_not_show_the_old_name(bot, monkeypatch):
    adapter, gate, calls = bot
    gate.set()
    telegram._get_bot_username()
    _wait_for(lambda: telegram._get_bot_username() == "bot_123")
    monkeypatch.setattr(adapter, "token", "987:other")
    assert telegram._get_bot_username() is None
    _wait_for(lambda: telegram._get_bot_username() == "bot_987")
