"""var.0.0.4.86 (#486): the request bot and the Telegram channel publisher are separate.

Owner decisions (16.11.2026): a separate publisher bot; the Telegram channel is a
brand target; no automatic migration — without a brand channel Telegram
publishing is not configured.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.models import Channel
from core.state import store

SECRET_PUBLISHER = "111:publisher-bot-secret"
SECRET_REQUESTS = "222:request-bot-secret"


@pytest.fixture
def channels(monkeypatch):
    saved = {}
    monkeypatch.setattr(store.repository, "list_channels",
                        lambda brand_id=None: [c for c in saved.values() if not brand_id or c.brand_id == brand_id])
    monkeypatch.setattr(store.repository, "save_channel", lambda channel: saved.__setitem__(channel.channel_id, channel))
    monkeypatch.setattr(store.repository, "get_channel", lambda channel_id: saved.get(channel_id))

    def add(channel_id, brand, target, channel_type="telegram", enabled=True):
        saved[channel_id] = Channel(channel_id=channel_id, brand_id=brand, channel_type=channel_type,
                                    target=target, enabled=enabled)
    return add


def _job(brand="brand01"):
    return SimpleNamespace(job_id="2026-000486", brand_id=brand, topic="t", character_id="c", priority=5,
                           output_path="", active_video_version=1, script={}, publish_retry_count={},
                           publish_intent={}, publish_delivery_contract=None)


class TestBrandTarget:
    def test_the_target_is_the_brands_telegram_channel(self, channels):
        from core.api.job_helpers.publish import _publish_task_for
        channels("a", "brand01", "@brand_one_news")
        channels("b", "brand02", "@brand_two_news")
        channels("c", "brand01", "@disabled_news", enabled=False)
        channels("d", "brand01", "UCyoutube", channel_type="youtube")
        task = _publish_task_for(_job("brand01"), "telegram")
        assert task["metadata"]["telegram_chat_id"] == "@brand_one_news"
        assert task["publish_intent"]["target"] == "@brand_one_news"

    def test_the_chosen_brand_channel_wins_and_a_retry_keeps_it(self, channels):
        from core.api.job_helpers.publish import _publish_task_for
        channels("a", "brand01", "@first_channel")
        channels("b", "brand01", "@second_channel")
        job = _job()
        assert _publish_task_for(job, "telegram", "@second_channel")["metadata"]["telegram_chat_id"] == "@second_channel"
        retry = _publish_task_for(job, "telegram")
        assert retry["metadata"]["telegram_chat_id"] == "@second_channel"

    def test_no_brand_channel_is_not_configured_and_nothing_is_migrated(self, channels, monkeypatch, tmp_path):
        from core.api.job_helpers.publish import _publish_task_for
        from publishers.telegram import TelegramPublisher
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
        # A former installation-wide setting is ignored (owner decision: no migration).
        (tmp_path / "publishing-settings.json").write_text(json.dumps(
            {"public_url": "", "telegram_channel_id": "@old_global_channel"}), encoding="utf-8")
        task = _publish_task_for(_job(), "telegram")
        assert task["metadata"]["telegram_chat_id"] == ""
        monkeypatch.delenv("PUBLISHER_MOCK", raising=False)
        monkeypatch.setenv("TELEGRAM_PUBLISHER_BOT_TOKEN", SECRET_PUBLISHER)
        receipt = TelegramPublisher().publish("", task["metadata"])
        assert receipt == {"channel": "telegram", "status": "NOT_CONFIGURED",
                           "error": "Telegram-канал бренду не налаштовано"}

    def test_the_publishing_settings_no_longer_hold_a_telegram_channel(self, monkeypatch, tmp_path):
        from core.publishing_settings import publishing_settings
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
        (tmp_path / "publishing-settings.json").write_text(json.dumps(
            {"public_url": "https://v.example", "telegram_channel_id": "@old"}), encoding="utf-8")
        assert publishing_settings() == {"public_url": "https://v.example"}


class TestBrandChannelApi:
    @pytest.mark.parametrize("target,status", [("@valid_channel", 200), ("-1001234567890", 200),
                                               ("not a channel", 400), ("@abc", 400)])
    def test_a_telegram_brand_channel_must_be_a_channel_id(self, channels, target, status):
        response = TestClient(app).post("/api/brands/brand01/channels", json={
            "brand_id": "brand01", "channel_type": "telegram", "target": target})
        assert response.status_code == status, response.text

    def test_other_platform_targets_are_unchanged(self, channels):
        response = TestClient(app).post("/api/brands/brand01/channels", json={
            "brand_id": "brand01", "channel_type": "youtube", "target": "not a telegram id"})
        assert response.status_code == 200


def _installed(monkeypatch, root):
    monkeypatch.setenv("CONFIG_ROOT", str(root))
    (root / "installation.json").write_text('{"completed_at":"2026-01-01"}', encoding="utf-8")
    (root / "users.json").write_text("{}", encoding="utf-8")


class TestSeparateBots:
    def test_a_telegram_publish_task_gets_only_the_publisher_bot(self, monkeypatch, tmp_path):
        from core import first_run
        from core.api import tasks
        _installed(monkeypatch, tmp_path)
        first_run.set_integration_secret("telegram_bot_token", SECRET_REQUESTS)
        first_run.set_integration_secret("telegram_publisher_bot_token", SECRET_PUBLISHER)
        monkeypatch.setattr(tasks.store, "jobs", {"j": SimpleNamespace(publish_task_ids={"t": "telegram"})})
        monkeypatch.setattr(tasks.store, "workers", {"n": {"current_job": "j", "current_task": "t"}})
        monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, request: True)
        answer = TestClient(app).get("/api/tasks/t/secrets", headers={"x-vertep-node": "n"}).json()
        assert answer["secrets"] == {"telegram_publisher_bot_token": SECRET_PUBLISHER}
        assert SECRET_REQUESTS not in json.dumps(answer)

    def test_the_request_bot_token_cannot_be_written_by_a_publish_task(self, monkeypatch, tmp_path):
        from core import first_run
        from core.api import tasks
        _installed(monkeypatch, tmp_path)
        monkeypatch.setattr(tasks.store, "jobs", {"j": SimpleNamespace(publish_task_ids={"t": "telegram"})})
        monkeypatch.setattr(tasks.store, "workers", {"n": {"current_job": "j", "current_task": "t"}})
        monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, request: True)
        response = TestClient(app).put("/api/tasks/t/secrets", headers={"x-vertep-node": "n"},
                                       json={"secrets": {"telegram_bot_token": "x"}})
        assert response.status_code == 403

    def test_the_publisher_uses_its_own_credential(self):
        from adapters.publisher import PUBLISHERS
        from publishers import LIVE_PUBLISHERS
        assert PUBLISHERS["telegram"].credential_env == "TELEGRAM_PUBLISHER_BOT_TOKEN"
        assert type(PUBLISHERS["telegram"]) is type(LIVE_PUBLISHERS["telegram"])  # one implementation

    def test_the_publisher_secret_is_a_known_integration_secret(self):
        from core.first_run import INTAKE_ONLY_SECRET_NAMES, INTEGRATION_SECRET_NAMES
        assert "telegram_publisher_bot_token" in INTEGRATION_SECRET_NAMES
        assert INTAKE_ONLY_SECRET_NAMES == {"telegram_bot_token"}
