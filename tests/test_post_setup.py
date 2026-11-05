"""Manual post-install steps: what stays pending, and how an update and the wizard meet them."""

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import post_setup
from core.app import app


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setattr(post_setup, "config_root", lambda: tmp_path)
    monkeypatch.setattr(post_setup, "ensure_secret_store", lambda: {})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "")
    monkeypatch.setattr("core.telegram_store._path", lambda: tmp_path / "no-telegram.json")
    return tmp_path


def test_nothing_pending_without_key_or_bot(config):
    assert post_setup.post_setup_state() == {"pending": False, "telegram": False, "backup_key": False}


def test_key_stays_pending_until_acknowledged(config):
    (config / "backup.key").write_text("secret", encoding="utf-8")
    assert post_setup.post_setup_state()["backup_key"] is True
    assert post_setup.acknowledge_backup_key()["pending"] is False


def test_bot_without_any_chat_is_pending_until_a_chat_is_set(config, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert post_setup.post_setup_state()["telegram"] is True
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "1")
    assert post_setup.post_setup_state()["telegram"] is False


def test_api_downloads_the_key_uncached(config):
    (config / "backup.key").write_text("secret", encoding="utf-8")
    client = TestClient(app)
    assert client.get("/api/system/post-setup").json()["backup_key"] is True
    response = client.post("/api/system/post-setup/backup-key")
    assert response.status_code == 200 and response.text == "secret"
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/api/system/post-setup/backup-key/saved").json()["backup_key"] is False


def test_update_sets_the_production_defaults(tmp_path):
    spec = importlib.util.spec_from_file_location("runtime_env", "scripts/update-runtime-env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = tmp_path / ".env"
    env.write_text("DEMO_MODE=true\nKEEP=1\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    images = {"comfyui": {"reference": "r", "digest": "d"}}
    manifest.write_text(json.dumps({"version": "9.9", "images": images}), encoding="utf-8")
    module.update(env, manifest, "9.9")
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "DEMO_MODE=false" in lines and "DEMO_MODE=true" not in lines
    assert "REQUIRE_UPDATE_KEY_LIFECYCLE=true" in lines and "KEEP=1" in lines


def test_wizard_asks_for_telegram_and_makes_the_key_a_required_step():
    html = Path("web/setup.html").read_text(encoding="utf-8")
    for element in ('id="telegramToken"', 'id="telegramAdmins"', 'id="backupKeyDownload"', 'id="backupKeySaved"'):
        assert element in html
    assert "telegram_admin_chat_ids" in html
