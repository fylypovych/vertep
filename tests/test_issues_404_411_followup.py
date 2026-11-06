"""Remaining acceptance of i.0.0.4.4-i.0.0.4.11 at the real call sites."""
import asyncio
import importlib.util
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from core import runtime
from core.models import JobStatus
from core.telegram import handlers
from publishers import FakeTransport
from publishers.base import fetch_permalink
from publishers.tiktok import TikTokPublisher


def test_watchdog_uses_a_thread_and_prepares_a_job_once(monkeypatch):
    job = SimpleNamespace(job_id="j1", status=JobStatus.NEW)
    threads, submitted, ticks = [], [], []

    async def sleep(_):
        ticks.append(1)
        if len(ticks) > 2:
            raise asyncio.CancelledError

    async def to_thread(function, *args):
        threads.append(function)
        return True

    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)
    monkeypatch.setattr(runtime.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(runtime, "dispatch_allowed", lambda: True)
    monkeypatch.setattr(runtime.task_queue, "acquire_watchdog_lock", lambda ttl: True)
    monkeypatch.setattr(runtime.task_queue, "requeue_expired", lambda: [])
    monkeypatch.setattr(runtime, "_recover_stale_workers", lambda: None)
    monkeypatch.setattr(runtime, "advance_rollout", lambda: None)
    monkeypatch.setattr(runtime, "cleanup_jobs", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "cleanup_temporary_files", lambda *a, **k: None)
    monkeypatch.setattr(runtime.store, "jobs", {"j1": job})
    monkeypatch.setattr(runtime.store, "update", lambda *a, **k: job)
    monkeypatch.setattr(runtime, "_submit_prepare",
                        lambda item: (submitted.append(item.job_id), runtime._preparing.add(item.job_id)))
    monkeypatch.setattr(runtime, "_preparing", set())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime._watchdog())
    assert submitted == ["j1"]
    assert runtime._job_is_due in threads


def test_telegram_cannot_create_a_job_during_update(monkeypatch):
    from core import system_state
    monkeypatch.setattr(system_state, "jobs_may_be_created", lambda: False)
    monkeypatch.setattr(handlers.store, "jobs", {})
    monkeypatch.setattr(handlers.store.repository, "has_telegram_update", lambda *a: False)
    pending = {"text": "тема", "source_id": "1", "message": {"chat": {"id": 5}}}
    with pytest.raises(HTTPException) as error:
        handlers._create_job_from_telegram(pending, "brand01", "did_samogon")
    assert "тимчасово вимкнено" in error.value.detail


def test_lost_permalink_answer_never_fails_a_published_post():
    transport = FakeTransport([httpx.Response(500, json={})])
    assert fetch_permalink(transport, "https://graph.example/1", "t") is None


def test_tiktok_receipt_has_no_invented_link(monkeypatch, tmp_path):
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "t")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    fake = FakeTransport([
        httpx.Response(200, json={"data": {"upload_url": "https://upload.example/s", "publish_id": "p"}}),
        httpx.Response(200, json={"status_code": 0}),
        httpx.Response(200, json={"data": {"status": "SEND_TO_USER_FEED"}}),
    ])
    result = TikTokPublisher(transport=fake).publish(str(video), {"title": "т", "privacy": "public"})
    assert result["url"] == "" and result["permalink_available"] is False


def test_production_mock_flag_does_not_fake_a_post(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.delenv("TIKTOK_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("publishers.oauth._get_secret", lambda name: None)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    result = TikTokPublisher(transport=FakeTransport([])).publish(str(video), {"title": "т"})
    assert result["status"] != "PUBLISHED"


def _status_line(monkeypatch):
    monkeypatch.setattr(handlers, "_call_core_api", lambda *a, **k: None)
    monkeypatch.setattr(handlers, "_format_last_backup", lambda: "")
    return next(line for line in handlers._system_status_text().splitlines() if line.startswith("Dispatcher"))


def test_dispatcher_indicator_reports_the_service(monkeypatch):
    monkeypatch.setenv("DISPATCHER_URL", "http://dispatcher:8094")
    monkeypatch.setattr(handlers.httpx, "get", lambda url, timeout: httpx.Response(
        200, request=httpx.Request("GET", url)))
    assert _status_line(monkeypatch) == "Dispatcher: running"

    def down(url, timeout):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(handlers.httpx, "get", down)
    assert _status_line(monkeypatch) == "Dispatcher: error"


def test_generator_keeps_a_recorded_owner_decision():
    spec = importlib.util.spec_from_file_location("trace_411", "scripts/issue-122-retirement-trace.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build_trace.__defaults__ == (None,)
