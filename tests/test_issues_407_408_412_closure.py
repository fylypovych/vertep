"""Closing acceptance gaps of i.0.0.4.7, i.0.0.4.8 and i.0.0.4.12."""
import base64
import importlib.util
import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx

from core import first_run
from core.api import tasks
from core.logging_config import secret_redact
from publishers import FakeTransport
from publishers.telegram import TelegramPublisher
from worker import role_executor

SECRET = "123456:SUPER-secret-token"
TASK = {"task": "publish", "channel": "youtube", "topic": "x", "job_id": "j", "video_path": "", "metadata": {}}


def _receipt(artifact):
    return json.loads(base64.b64decode(artifact["data_base64"]).decode("utf-8"))


def test_execute_publisher_does_not_fake_a_post_outside_demo(monkeypatch):
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.delenv("YOUTUBE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("publishers.oauth._get_secret", lambda name: None)
    [artifact] = role_executor.execute_role_task("publisher", dict(TASK))
    assert _receipt(artifact)["status"] != "PUBLISHED"


def test_execute_publisher_mock_stays_available_in_demo(monkeypatch):
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    monkeypatch.setenv("DEMO_MODE", "true")
    [artifact] = role_executor.execute_role_task("publisher", dict(TASK))
    assert _receipt(artifact)["status"] == "PUBLISHED"


def test_generator_writes_the_recorded_decision():
    spec = importlib.util.spec_from_file_location("trace_closure", "scripts/issue-122-retirement-trace.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = Path("reports/issue-122/issue-122-p12-retirement.json")
    recorded = json.loads(report.read_text(encoding="utf-8"))["owner_decision"]
    assert "#321" in recorded
    assert recorded in module.render_markdown({
        "summary": {"rows": 0, "kept": 0, "retire_with_evidence": 0, "blocked": 0, "duplicate_issues": []},
        "rows": [], "owner_decision": recorded})


def test_telegram_token_in_a_url_is_masked():
    text = secret_redact(f"500 for url https://api.telegram.org/bot{SECRET}/sendVideo")
    assert SECRET not in text and "SUPER-secret" not in text


def test_failed_telegram_upload_does_not_leak_the_token(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")

    class Failing(FakeTransport):
        def post(self, url, **kwargs):
            raise httpx.ConnectError(f"cannot reach {url}")

    result = TelegramPublisher(transport=Failing([])).publish(str(video), {"telegram_chat_id": "@vertep"})
    assert result["status"] == "FAILED"
    assert "SUPER-secret" not in json.dumps(result)


def _database(monkeypatch, root):
    table = {}
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "host=postgres")
    monkeypatch.setattr(first_run, "config_root", lambda: root)
    monkeypatch.setattr(first_run, "_database_envelope", lambda dsn: table.get("row"))
    monkeypatch.setattr(first_run, "_write_database_envelope",
                        lambda dsn, envelope: table.__setitem__("row", json.loads(json.dumps(envelope))))
    return table


def test_secret_access_writes_no_value_to_the_log(monkeypatch, tmp_path, caplog):
    _database(monkeypatch, tmp_path)
    caplog.set_level(logging.DEBUG)
    first_run.set_integration_secret("youtube_access_token", SECRET)
    monkeypatch.setattr(tasks.store, "jobs", {"j": SimpleNamespace(publish_task_ids={"t": "youtube"})})
    monkeypatch.setattr(tasks.store, "workers", {"n": {"current_job": "j", "current_task": "t"}})
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, request: True)
    assert tasks.task_secrets("t", SimpleNamespace(headers={"x-vertep-node": "n"}))["secrets"]
    assert SECRET not in caplog.text


def test_restored_row_and_key_open_on_a_new_host(monkeypatch, tmp_path):
    """A snapshot carries the database row and the sealed key from config: both are enough."""
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    table = _database(monkeypatch, old)
    first_run.set_integration_secret("youtube_access_token", SECRET)
    shutil.copytree(old, new)
    row = table["row"]
    monkeypatch.setattr(first_run, "config_root", lambda: new)
    table.clear()
    table["row"] = row
    assert first_run.ensure_secret_store()["youtube_access_token"] == SECRET


def test_snapshot_keeps_the_store_key():
    source = Path("services/backup_service.py").read_text(encoding="utf-8")
    assert '_SNAPSHOT_EXCLUDED = {"backup.key"}' in source
