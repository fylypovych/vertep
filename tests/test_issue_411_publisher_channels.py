"""i.0.0.4.11: Telegram runs on the Worker; Meta channels wait and report real links."""
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from adapters.providers import DefaultPublisherProvider
from core.api import job_helpers, jobs
from publishers import LIVE_PUBLISHERS
from publishers.instagram import InstagramPublisher
from publishers.telegram import MAX_UPLOAD_BYTES, TelegramPublisher
from publishers.threads import ThreadsPublisher


class Transport:
    def __init__(self, posts, gets=()):
        self.posts, self.gets, self.calls = list(posts), list(gets), []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return httpx.Response(200, json=self.posts.pop(0), request=httpx.Request("POST", url))

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return httpx.Response(200, json=self.gets.pop(0), request=httpx.Request("GET", url))


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"video")
    return str(path)


def test_core_and_worker_agree_on_channels():
    assert set(LIVE_PUBLISHERS) == set(DefaultPublisherProvider().available_channels())


def test_telegram_public_channel_has_a_real_link(monkeypatch, video):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    transport = Transport([{"result": {"message_id": 7, "chat": {"username": "vertep"}}}])
    receipt = TelegramPublisher(transport)._publish_live(video, {"topic": "тема", "telegram_chat_id": "@vertep"})
    assert receipt["url"] == "https://t.me/vertep/7"
    assert transport.calls[0][1].endswith("/sendVideo")


def test_telegram_private_channel_declares_no_link(monkeypatch, video):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    transport = Transport([{"result": {"message_id": 7, "chat": {}}}])
    receipt = TelegramPublisher(transport)._publish_live(video, {"telegram_chat_id": "-100"})
    assert receipt["url"] == "" and receipt["permalink_available"] is False


def test_telegram_refuses_a_file_over_the_limit(monkeypatch, video):
    monkeypatch.setattr("os.path.getsize", lambda path: MAX_UPLOAD_BYTES + 1)
    with pytest.raises(ValueError, match="50 MB"):
        TelegramPublisher(Transport([]))._publish_live(video, {"telegram_chat_id": "@vertep"})


def _meta(monkeypatch):
    monkeypatch.setenv("PUBLISHER_CONTAINER_POLL_SECONDS", "0")
    monkeypatch.setattr("publishers.instagram.InstagramTokenProvider",
                        lambda: SimpleNamespace(access_token=lambda: "tok"))
    monkeypatch.setattr("publishers.threads.ThreadsTokenProvider",
                        lambda: SimpleNamespace(access_token=lambda: "tok"))


def test_instagram_uploads_the_file_waits_and_reads_the_permalink(monkeypatch, video):
    _meta(monkeypatch)
    transport = Transport([{"id": "c1"}, {"success": True}, {"id": "m1"}],
                          [{"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"},
                           {"permalink": "https://www.instagram.com/reel/abc/"}])
    receipt = InstagramPublisher(transport)._publish_live(video, {"instagram_user_id": "1"})
    assert receipt["url"] == "https://www.instagram.com/reel/abc/"
    assert "video_url" not in transport.calls[0][2]["data"]
    assert "rupload.facebook.com" in transport.calls[1][1]
    assert transport.calls[1][2]["content"] == b"video"
    assert transport.calls[-2][1].endswith("/media_publish")


def test_failed_container_is_never_published(monkeypatch, video):
    _meta(monkeypatch)
    transport = Transport([{"id": "c1"}, {"success": True}], [{"status_code": "ERROR"}])
    with pytest.raises(RuntimeError, match="ERROR"):
        InstagramPublisher(transport)._publish_live(video, {"instagram_user_id": "1"})
    assert not any(call[1].endswith("/media_publish") for call in transport.calls)


def test_threads_without_public_address_explains_the_setting(monkeypatch, video):
    _meta(monkeypatch)
    with pytest.raises(ValueError, match="Публікація"):
        ThreadsPublisher(Transport([]))._publish_live(video, {"threads_user_id": "1"})


def test_threads_waits_and_reads_the_permalink(monkeypatch, video):
    _meta(monkeypatch)
    transport = Transport([{"id": "c1"}, {"id": "p1"}],
                          [{"status": "FINISHED"}, {"permalink": "https://www.threads.com/@a/post/x"}])
    receipt = ThreadsPublisher(transport)._publish_live(
        video, {"threads_user_id": "1", "video_url": "https://core.example/v.mp4"})
    assert receipt["url"] == "https://www.threads.com/@a/post/x"


def _job():
    return SimpleNamespace(job_id="2026-000001", active_video_version=2)


def _settings_root(monkeypatch, tmp_path):
    monkeypatch.setattr("core.publishing_settings.config_root", lambda: tmp_path)


def test_admin_panel_values_reach_the_tasks(monkeypatch, tmp_path):
    from core.publishing_settings import publishing_settings, save_publishing_settings
    _settings_root(monkeypatch, tmp_path)
    monkeypatch.setenv("SESSION_SECRET", "s")
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    save_publishing_settings("https://core.example/", "@vertep")
    assert publishing_settings() == {"public_url": "https://core.example", "telegram_channel_id": "@vertep"}
    assert job_helpers._public_video_url(_job())["video_url"].startswith("https://core.example/api/public/")


@pytest.mark.parametrize("url,channel", [("http://core.example", ""), ("core.example", ""),
                                         ("https://core.example?x=1", ""), ("", "not a channel")])
def test_invalid_publishing_settings_are_refused(monkeypatch, tmp_path, url, channel):
    from core.publishing_settings import save_publishing_settings
    _settings_root(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        save_publishing_settings(url, channel)


def test_no_public_url_means_no_link(monkeypatch, tmp_path):
    _settings_root(monkeypatch, tmp_path)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "s")
    assert job_helpers._public_video_url(_job()) == {}


def test_link_serves_only_the_signed_active_version(monkeypatch, tmp_path):
    _settings_root(monkeypatch, tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    job = _job()
    monkeypatch.setenv("SESSION_SECRET", "s")
    monkeypatch.setenv("PUBLIC_URL", "https://core.example/")
    monkeypatch.setattr(jobs.store, "jobs", {job.job_id: job})
    monkeypatch.setattr(jobs, "_active_video_file", lambda job, root: video)
    url = job_helpers._public_video_url(job)["video_url"]
    assert url.startswith("https://core.example/api/public/videos/2026-000001/2/")
    expires, signature = url.rsplit("/", 2)[-2:]
    expires, signature = int(expires), signature.removesuffix(".mp4")
    assert jobs.public_video(job.job_id, 2, expires, signature).path == video
    for arguments in ((job.job_id, 1, expires, signature), (job.job_id, 2, expires + 1, signature),
                      (job.job_id, 2, expires, "0" * 64), ("other", 2, expires, signature)):
        with pytest.raises(HTTPException):
            jobs.public_video(*arguments)
    expired = int(time.time()) - 1
    with pytest.raises(HTTPException):
        jobs.public_video(job.job_id, 2, expired, job_helpers._public_video_signature(job.job_id, 2, expired))
