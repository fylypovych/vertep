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
    _registry(monkeypatch, _instance("publishing"))
    client = TestClient(app, raise_server_exceptions=False)
    auth = ("admin", __import__("os").getenv("ADMIN_PASSWORD", "test-admin-pw-long"))
    for response in (client.get("/api/characters", auth=auth), client.post("/api/brands/x/channels", auth=auth, json={})):
        path = response.request.url.path
        assert response.status_code == 503 and response.json()["code"] == "module_is_not_active", path
    _registry(monkeypatch, _instance("characters"))
    assert client.get("/api/characters", auth=auth).status_code == 200  # READY: served
