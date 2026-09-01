import json

from fastapi.testclient import TestClient

from core import app as core_app
from core.system_state import SystemState, get_system_state, set_system_state


def test_brand_and_workflow_delete_endpoints(monkeypatch, tmp_path):
    brand_root = tmp_path / "brands"
    workflow_root = tmp_path / "workflows"
    character_root = tmp_path / "characters"
    monkeypatch.setenv("BRANDS_ROOT", str(brand_root))
    monkeypatch.setenv("CHARACTERS_ROOT", str(character_root))
    monkeypatch.setattr(core_app.workflow_registry, "root", workflow_root)
    client = TestClient(core_app.app)

    brand = {"id": "test_brand", "name": "Тестовий бренд", "enabled": True,
             "metadata": {"language": "uk"}, "publishing": {}}
    assert client.put("/api/brands/test_brand", json=brand).status_code == 200
    assert client.delete("/api/brands/test_brand").json() == {"deleted": "test_brand"}

    workflow = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "vertep"}}}
    assert client.put("/api/workflows/image/test.json", json=workflow).status_code == 200
    assert client.delete("/api/workflows/image/test.json").json() == {
        "deleted": "workflows/image/test.json"}


def test_workflow_delete_rejects_character_reference(monkeypatch, tmp_path):
    workflow_root = tmp_path / "workflows"
    character_root = tmp_path / "characters"
    character = character_root / "hero"
    character.mkdir(parents=True)
    (character / "generation.json").write_text(
        json.dumps({"workflow": "workflows/image/test.json"}), encoding="utf-8")
    monkeypatch.setenv("CHARACTERS_ROOT", str(character_root))
    monkeypatch.setattr(core_app.workflow_registry, "root", workflow_root)
    core_app.workflow_registry.save(
        "image", "test.json", {"1": {"class_type": "SaveImage", "inputs": {}}})

    response = TestClient(core_app.app).delete("/api/workflows/image/test.json")
    assert response.status_code == 409
    assert "персонаж hero" in response.text


def test_emergency_reason_and_update_log_are_visible_in_alerts(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    set_system_state(SystemState.EMERGENCY, "Update and rollback failed", "operation-1")
    (tmp_path / "status.json").write_text(json.dumps({
        "state": "FAILED", "phase": "EMERGENCY", "request_id": "operation-1",
        "message": "database connection was terminated", "log": ["rollback failed"],
    }), encoding="utf-8")

    alerts = TestClient(core_app.app).get("/api/alerts").json()
    assert next(item for item in alerts if item["type"] == "SYSTEM_STATE")["message"] == \
        "Update and rollback failed"
    update = next(item for item in alerts if item["type"] == "UPDATE_FAILED")
    assert update["details"] == ["rollback failed"]


def test_administrator_can_recover_only_after_health_checks(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    set_system_state(SystemState.EMERGENCY, "rollback failed")
    monkeypatch.setattr(core_app, "system_status", lambda: {
        "core": "OK", "postgres": "OK", "redis": "OK"})

    response = TestClient(core_app.app).post("/api/system/recovery/normal")
    assert response.status_code == 200
    assert get_system_state()["state"] == "NORMAL"
