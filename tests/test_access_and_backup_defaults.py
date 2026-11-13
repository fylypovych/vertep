"""Fail-closed Telegram access and the standard backup-service configuration."""

import base64
import importlib.util
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.app import app
from core.dispatcher import _voice_ready
from core.models import WorkerHeartbeat
from core.telegram import handlers
from core.telegram_store import is_allowed_chat
from services import backup_service
from tests.telegram_patching import patch_telegram


def available_voice(worker):
    return _voice_ready(worker, {"voice": "uk"})


def _allow(monkeypatch, allowed="", admins=""):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", allowed)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", admins)


def test_empty_allowlist_admits_only_admins(monkeypatch):
    _allow(monkeypatch, admins="1")
    assert is_allowed_chat("1") and not is_allowed_chat("2")


def test_control_commands_require_admin(monkeypatch):
    _allow(monkeypatch, allowed="2", admins="1")
    sent, cancelled = [], []
    patch_telegram(monkeypatch, "TelegramAdapter",
                        lambda: SimpleNamespace(send_message=lambda chat, text: sent.append(text)))
    patch_telegram(monkeypatch, "cancel_job", cancelled.append)
    handlers.store.jobs["j"] = SimpleNamespace(job_id="j", status=SimpleNamespace(value="NEW"))
    try:
        handlers._handle_telegram_message("2", "s", "/cancel j", {})
        assert cancelled == [] and sent == ["Доступ заборонено."]
        handlers._handle_telegram_message("1", "s", "/cancel j", {})
        assert cancelled == ["j"]
    finally:
        handlers.store.jobs.pop("j")


def test_callback_from_unlisted_chat_is_refused(monkeypatch):
    _allow(monkeypatch, allowed="2")
    answers = []
    patch_telegram(monkeypatch, "TelegramAdapter",
                        lambda: SimpleNamespace(answer_callback=lambda cid, text: answers.append(text)))
    handlers._handle_telegram_callback({"id": "c", "data": "vid_ok:x:1", "message": {"chat": {"id": 9}}})
    assert answers == ["Доступ заборонено."]


def test_webhook_without_configured_secret_is_refused(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_ENABLED", "true")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    response = TestClient(app).post("/api/telegram/webhook", json={"message": {"text": "x", "chat": {"id": 1}}})
    assert response.status_code == 401


def test_backup_service_can_reach_core_and_databases():
    compose = yaml.safe_load(Path("deploy/docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"]["backup-service"]["environment"]
    assert env["BACKUP_CORE_URL"] == "http://core:8080"
    assert env["PGPASSWORD"] and env["BACKUP_REDIS_LOGICAL"] == "true"
    assert env["BACKUP_PG_DUMP_PATH"].startswith("/data/backups/")
    assert env["BACKUP_REDIS_DUMP_PATH"].startswith("/data/backups/")
    assert "size=512m" in compose["services"]["backup-service"]["tmpfs"][0]


def test_snapshot_excludes_the_encryption_key(monkeypatch, tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "backup.key").write_text("k", encoding="utf-8")
    (config / "app.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(backup_service, "_sources", lambda: [("config", config)])
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")
    monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
    target = tmp_path / "s.tar.gz"
    backup_service._archive(target)
    with tarfile.open(target) as archive:
        names = archive.getnames()
    assert "config/app.json" in names and "config/backup.key" not in names


def test_worker_without_voice_catalog_is_not_ready():
    assert not available_voice({}) and not available_voice({"voice_catalog": {}})
    assert available_voice({"voice_catalog": {"voices": ["uk"]}})


def test_heartbeat_keeps_scheduler_tags():
    data = WorkerHeartbeat(node_name="n", scheduler_tags=["fast"]).model_dump()
    assert data["scheduler_tags"] == ["fast"]


def test_bootstrap_requires_update_key_lifecycle():
    assert "\nREQUIRE_UPDATE_KEY_LIFECYCLE=true\n" in Path("bootstrap.sh").read_text(encoding="utf-8")


def test_startup_recovery_keeps_active_restore_fence(tmp_path):
    spec = importlib.util.spec_from_file_location("startup_recovery", "scripts/startup-recovery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "system-state.json"
    state.write_text(json.dumps({"state": "MAINTENANCE", "operation_id": "restore:s"}), encoding="utf-8")
    module.restore_system_state(tmp_path, tmp_path)
    assert json.loads(state.read_text(encoding="utf-8"))["state"] == "MAINTENANCE"
    state.write_text(json.dumps({"state": "MAINTENANCE"}), encoding="utf-8")
    module.restore_system_state(tmp_path, tmp_path)
    assert json.loads(state.read_text(encoding="utf-8"))["state"] == "NORMAL"


class FakeRedis:
    def __init__(self):
        self.data = {b"queue": (b"payload", -1), b"lease": (b"held", 5000)}

    def scan_iter(self, count=0):
        return list(self.data)

    def dump(self, key):
        return self.data[key][0]

    def pttl(self, key):
        return self.data[key][1]

    def flushdb(self):
        self.data = {}

    def restore(self, key, ttl, value, replace=False):
        self.data[key] = (value, ttl or -1)


def _backup_env(monkeypatch, tmp_path, part_size="64"):
    config = tmp_path / "config"
    config.mkdir()
    (config / "app.json").write_text("original" * 200, encoding="utf-8")
    fake = FakeRedis()
    monkeypatch.setattr("redis.from_url", lambda url: fake)
    monkeypatch.setattr(backup_service, "_sources", lambda: [("config", config)])
    monkeypatch.setattr(backup_service, "_backup_root", lambda: tmp_path / "backups")
    monkeypatch.setattr(backup_service, "_restore_state_dir", lambda: tmp_path / "state")
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(backup_service, "_is_restore_allowed", lambda: (True, ""))
    monkeypatch.setattr(backup_service, "_core_available", lambda: True)
    monkeypatch.setattr(backup_service, "_set_system_state", lambda *a, **k: True)
    monkeypatch.setattr(backup_service, "_post_restore_health_check", lambda destinations: None)
    for name, value in (("BACKUP_PG_DUMP_CMD", ""), ("BACKUP_REDIS_DUMP_CMD", ""), ("BACKUP_REDIS_LOGICAL", "true"),
                        ("REDIS_URL", "redis://fake"), ("BACKUP_PART_SIZE_BYTES", part_size),
                        ("BACKUP_REDIS_DUMP_PATH", str(tmp_path / "keys.jsonl")),
                        ("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"k" * 32).decode())):
        monkeypatch.setenv(name, value)
    return config, fake


def test_large_snapshot_splits_and_restores_with_pre_copy_and_redis(monkeypatch, tmp_path):
    config, fake = _backup_env(monkeypatch, tmp_path)
    receipt = backup_service._create_snapshot("job")
    root = tmp_path / "backups"
    assert len(receipt["parts"]) > 1
    assert all((root / name).stat().st_size <= 64 for name in receipt["parts"])
    assert len(list(root.glob("*.vtbackup"))) == 1
    (config / "app.json").write_text("changed", encoding="utf-8")
    fake.data = {b"stale": (b"x", -1)}
    result = backup_service._execute_restore(receipt["snapshot_id"])
    assert (config / "app.json").read_text(encoding="utf-8") == "original" * 200
    assert fake.data[b"queue"] == (b"payload", -1) and b"stale" not in fake.data
    pre = json.loads((root / f"{result['pre_restore_snapshot']}.json").read_text(encoding="utf-8"))
    assert pre["job_id"] == f"pre-restore:{receipt['snapshot_id']}"


def test_missing_part_blocks_restore(monkeypatch, tmp_path):
    _backup_env(monkeypatch, tmp_path)
    receipt = backup_service._create_snapshot("job")
    (tmp_path / "backups" / receipt["parts"][-1]).unlink()
    with pytest.raises(HTTPException) as refused:
        backup_service._execute_restore(receipt["snapshot_id"])
    assert refused.value.status_code == 409


def test_failed_pre_restore_copy_leaves_data_untouched(monkeypatch, tmp_path):
    config, _ = _backup_env(monkeypatch, tmp_path, part_size="536870912")
    receipt = backup_service._create_snapshot("job")
    (config / "app.json").write_text("changed", encoding="utf-8")
    monkeypatch.setattr(backup_service, "_redis_export", lambda path: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(HTTPException):
        backup_service._execute_restore(receipt["snapshot_id"])
    assert (config / "app.json").read_text(encoding="utf-8") == "changed"


def test_redis_export_import_on_a_real_server(monkeypatch, tmp_path):
    url = os.getenv("VERTEP_TEST_REDIS_URL", "").strip()
    if not url:
        pytest.skip("VERTEP_TEST_REDIS_URL is not set")
    import redis
    url = f"{url.rsplit('/', 1)[0]}/14"  # a database number in the URL overrides db=
    client = redis.from_url(url)
    monkeypatch.setenv("REDIS_URL", url)
    client.flushdb()
    client.set("plain", "v")
    client.set("expiring", "v", ex=100)
    client.rpush("queue", "a", "b")
    backup_service._redis_export(tmp_path / "keys.jsonl")
    client.flushdb()
    client.set("stale", "x")
    backup_service._redis_import(tmp_path / "keys.jsonl")
    assert client.get("stale") is None and client.get("plain") == b"v"
    assert client.lrange("queue", 0, -1) == [b"a", b"b"] and 0 < client.ttl("expiring") <= 100
    client.flushdb()
