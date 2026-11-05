"""i.0.0.4.7 / i.0.0.4.8: no fake publication outside demo; Telegram reports real counts."""
from types import SimpleNamespace

from adapters.telegram import TelegramAdapter
from core.api.job_helpers import _validate_publication_receipt
from core.models import JobStatus
from core.telegram import handlers
from publishers.base import publisher_mock_enabled

MOCK = {"channel": "youtube", "status": "PUBLISHED", "remote_id": "mock-1", "timestamp": 1.0,
        "url": "https://example.invalid/youtube/mock-1", "upload": {"mode": "mock"}}


def test_mock_requires_demo_mode(monkeypatch):
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    monkeypatch.setenv("DEMO_MODE", "false")
    assert publisher_mock_enabled() is False
    monkeypatch.setenv("DEMO_MODE", "true")
    assert publisher_mock_enabled() is True


def test_core_rejects_mock_receipt_outside_demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    valid, error = _validate_publication_receipt(MOCK, "youtube")
    assert valid is False and "Mock" in error
    monkeypatch.setenv("DEMO_MODE", "true")
    assert _validate_publication_receipt(MOCK, "youtube") == (True, None)


def test_receipt_without_permalink_must_declare_it(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    receipt = {"channel": "tiktok", "status": "PUBLISHED", "remote_id": "7", "timestamp": 1.0, "url": ""}
    assert _validate_publication_receipt(receipt, "tiktok")[0] is False
    receipt["permalink_available"] = False
    assert _validate_publication_receipt(receipt, "tiktok") == (True, None)


def test_status_counts_real_jobs_and_nodes(monkeypatch):
    jobs = {"a": SimpleNamespace(status=JobStatus.NEW), "b": SimpleNamespace(status=JobStatus.FAILED)}
    monkeypatch.setattr(handlers.store, "jobs", jobs)
    monkeypatch.setattr(handlers, "_call_core_api",
                        lambda method, path: [{"status": "ONLINE"}, {"status": "OFFLINE"}])
    assert handlers._format_workers_summary() == "Вузли: 1/2 online"
    summary = handlers._format_jobs_summary()
    assert "1 queued" in summary and "1 failed" in summary


def test_unknown_node_list_is_not_reported_as_zero(monkeypatch):
    monkeypatch.setattr(handlers, "_call_core_api", lambda method, path: None)
    assert handlers._format_workers_summary() == "Вузли: дані недоступні"


def test_message_without_token_is_marked_undelivered(monkeypatch):
    adapter = TelegramAdapter()
    adapter.token = ""
    assert adapter.send_message("1", "текст")["delivered"] is False
