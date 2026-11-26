"""Menu entries come from READY modules; pages and CORE API of a module close without it (owner, 23.11.2026)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.module import navigation
from core.module.lifecycle import bundled_manifest
from core.module.manifest import ModuleManifest

pytestmark = pytest.mark.strict_module_pages


def _instance(module_id: str, state: str = "READY", manifest: ModuleManifest | None = None):
    return SimpleNamespace(module_id=module_id, node_id="core", state=state,
                           manifest=manifest or ModuleManifest.from_dict(bundled_manifest(module_id)))


def _registry(monkeypatch, *instances):
    registry = SimpleNamespace(
        instances=lambda module_id=None: [i for i in instances if module_id in (None, i.module_id)],
        ready_instances=lambda module_id: [i for i in instances if i.module_id == module_id and i.state == "READY"])
    monkeypatch.setattr("core.module.gateway._modules_or_none", lambda: registry)


def _demo(navigation_entry: dict, files: bool = True) -> ModuleManifest:
    data = bundled_manifest("storyboard")
    data = {**data, "module_id": "demo", "name": "demo",
            "ui": {**data["ui"], "navigation": [navigation_entry], "files": data["ui"]["files"] if files else {}}}
    return ModuleManifest.from_dict(data)


def test_menu_lists_only_ready_modules_with_their_declared_pages(monkeypatch):
    _registry(monkeypatch, _instance("characters"), _instance("characters"), _instance("brands", "INACTIVE"),
              _instance("storyboard"))
    items = navigation.navigation()["items"]
    assert [(i["module_id"], i["path"], i["group"]) for i in items] == [
        ("characters", "/characters", "content"), ("storyboard", "/modules/storyboard", "modules")]


def test_a_module_cannot_point_its_entry_at_another_page(monkeypatch):
    _registry(monkeypatch, _instance("demo", manifest=_demo({"id": "demo", "label": "Демо", "route": "/settings"})),
              _instance("bare", manifest=_demo({"id": "bare", "route": "/settings"}, files=False)))
    # Its own signed UI instead; a module with neither has no entry.
    assert [(i["module_id"], i["path"]) for i in navigation.navigation()["items"]] == [("demo", "/modules/demo")]


@pytest.mark.parametrize("entry", [{"id": "x", "route": "/a/../b"}, {"id": "x", "route": 5},
                                   {"id": "x", "group": "two words"}, {"id": "x", "admin_only": "yes"}])
def test_manifest_rejects_an_invalid_menu_entry(entry):
    with pytest.raises(ValueError):
        _demo(entry)


def test_core_api_of_a_module_page_answers_503_without_the_module(monkeypatch):
    from core.app import app
    _registry(monkeypatch, _instance("publisher"))
    client = TestClient(app, raise_server_exceptions=False)
    auth = ("admin", __import__("os").getenv("ADMIN_PASSWORD", "test-admin-pw-long"))
    for response in (client.get("/api/characters", auth=auth), client.post("/api/brands/x/channels", auth=auth, json={})):
        path = response.request.url.path
        assert response.status_code == 503 and response.json()["code"] == "module_is_not_active", path
    _registry(monkeypatch, _instance("characters"))
    assert client.get("/api/characters", auth=auth).status_code == 200  # READY: served


def test_new_jobs_and_manual_publication_need_their_modules(monkeypatch):
    from core.app import app
    _registry(monkeypatch, _instance("characters"))  # Brands and Publishing are off
    client = TestClient(app, raise_server_exceptions=False)
    auth = ("admin", __import__("os").getenv("ADMIN_PASSWORD", "test-admin-pw-long"))
    for response in (client.post("/api/jobs", auth=auth, json={"topic": "Без брендів"}),
                     client.post("/api/jobs/missing/publish", auth=auth, json={"channels": ["youtube"]})):
        assert response.status_code == 503 and response.json()["code"] == "module_is_not_active", response.text


def test_telegram_offers_no_brand_or_character_choice_without_the_modules(monkeypatch):
    from unittest.mock import Mock
    import core.telegram.handlers as handlers
    from core.telegram.job_callbacks import _create_job_from_telegram
    from fastapi import HTTPException
    from tests.telegram_patching import patch_telegram
    _registry(monkeypatch, _instance("brands"))  # Characters is off
    adapter = Mock()
    patch_telegram(monkeypatch, "TelegramAdapter", lambda: adapter)
    patch_telegram(monkeypatch, "is_admin_chat", lambda chat_id: False)
    message = {"text": "Нова тема", "chat": {"id": "77"}, "message_id": 5}
    handlers._handle_telegram_message("77", "5", "Нова тема", message)
    text = adapter.send_message.call_args[0][1]
    assert "«Персонажі»" in text and len(adapter.send_message.call_args[0]) == 2  # no keyboard
    assert "77" not in handlers._telegram_pending_brands
    with pytest.raises(HTTPException) as refused:
        _create_job_from_telegram({"text": "t", "source_id": "6", "message": message}, "brand01", "did_samogon")
    assert refused.value.status_code == 503
