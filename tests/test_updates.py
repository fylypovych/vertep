import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import _hash_secret, app
from core.update_manager import request_update, update_status


def load_update_agent():
    path = Path("scripts/update-agent.py").resolve()
    spec = importlib.util.spec_from_file_location("vertep_update_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_request_is_atomic_and_single_flight(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    queued = request_update("check")
    assert queued["state"] == "PENDING"
    request_path = tmp_path / "requests" / f"{queued['request_id']}.json"
    assert json.loads(request_path.read_text(encoding="utf-8"))["action"] == "check"
    assert update_status()["pending"] == 1
    with pytest.raises(FileExistsError):
        request_update("update")
    assert not list(tmp_path.rglob("*.tmp"))


def test_web_update_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "false")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="disabled"):
        request_update("check")


def test_update_agent_accepts_only_configured_github_remote(monkeypatch):
    agent = load_update_agent()
    agent.validate_remote("git@github.com:owner/vertep.git", "github.com")
    agent.validate_remote("ssh://git@github.com/owner/vertep.git", "github.com")
    agent.validate_remote("https://github.com/owner/vertep.git", "github.com")
    with pytest.raises(RuntimeError, match="embedded credentials"):
        agent.validate_remote("https://token:secret@github.com/owner/vertep.git", "github.com")
    with pytest.raises(RuntimeError, match="not allowed"):
        agent.validate_remote("https://example.com/owner/vertep.git", "github.com")


def test_update_agent_persists_check_result_and_removes_request(monkeypatch, tmp_path):
    agent = load_update_agent()
    root = tmp_path / "repo"
    state = tmp_path / "state"
    root.mkdir()
    requests = state / "requests"
    requests.mkdir(parents=True)
    request_path = requests / ("a" * 32 + ".json")
    request_path.write_text(json.dumps({"request_id": "a" * 32, "action": "check"}), encoding="utf-8")
    monkeypatch.setattr(agent, "repository_status", lambda root, fetch=True: {
        "current_revision": "1" * 40, "remote_revision": "2" * 40, "ahead": 0, "behind": 1,
        "update_available": True, "dirty": False, "remote": "git@github.com:owner/vertep.git"})
    agent.process_request(root, state, request_path)
    result = json.loads((state / "status.json").read_text(encoding="utf-8"))
    assert result["state"] == "SUCCEEDED"
    assert result["update_available"] is True
    assert not request_path.exists()


def test_update_api_requires_admin_role(monkeypatch, tmp_path):
    password = "operator-password-long"
    monkeypatch.setenv("ADMIN_PASSWORD", "fallback-admin-password")
    monkeypatch.setenv("USERS_JSON", json.dumps({"operator": {
        "password_hash": _hash_secret(password), "role": "operator"}}))
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    client = TestClient(app)
    assert client.get("/api/system/update", auth=("operator", password)).status_code == 200
    assert client.post("/api/system/update/check", auth=("operator", password)).status_code == 403
    queued = client.post("/api/system/update/check", auth=("admin", "fallback-admin-password"))
    assert queued.status_code == 200
    assert queued.json()["state"] == "PENDING"
