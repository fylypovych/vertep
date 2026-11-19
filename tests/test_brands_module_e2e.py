"""#500 AC1/AC2, #502 AC2/AC3 (owner decision 19.11.2026): brands live in the
Brands Application Module.

Disposable PostgreSQL: the bundled package is installed/activated through the
platform lifecycle; the module's own HTTP handler serves CORE (via the module
token) and the Gateway; CORE's brand/channel API and consumers use the module;
legacy brands/channels migrate with their IDs; a shared channel is linked to
several brands without a copy; policies inherit or override; moderators change
only their brands; a READY module that stops answering fails closed.
"""

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import pg8000.dbapi
import pytest

from core.module.data import ModuleDatabase
from core.module.instances import ModuleInstanceRegistry, PlacementStore
from core.module.lifecycle import ModuleLifecycle, bundled_manifest
from core.module.manifest import ModuleManifest
from tests.module_postgres import module_postgres  # noqa: F401  (fixture)
from tests.test_characters_module import api  # noqa: F401  (fixture)

MODULE_DIR = Path(__file__).resolve().parents[1] / "modules" / "brands"


@pytest.fixture
def brands(api, tmp_path, monkeypatch, module_postgres):
    from core.module.gateway import issue_token
    monkeypatch.syspath_prepend(str(MODULE_DIR))
    import brands_module
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.4.11"}, PlacementStore(tmp_path / "placement.json"))
    credentials = {}
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda package: None, database=ModuleDatabase(module_postgres),
                                registry=registry, system_versions={"core_runtime": "0.0.4.11"},
                                store_credentials=lambda module_id, value: credentials.__setitem__(module_id, value))
    assert lifecycle.install_bundled("brands")["state"] == "INSTALLED"
    assert lifecycle.activate("brands")["state"] == "ACTIVE"
    parsed = urlparse(module_postgres)
    connection = pg8000.dbapi.connect(user=credentials["brands"]["role"], password=credentials["brands"]["password"],
                                      host=parsed.hostname, port=parsed.port, database=unquote(parsed.path.lstrip("/")))
    connection.autocommit = True
    monkeypatch.setattr(brands_module, "TOKEN", issue_token("brands", "text-node"))
    monkeypatch.setattr(brands_module, "_state", {"connection": connection, "store": None})
    server = ThreadingHTTPServer(("127.0.0.1", 0), brands_module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    manifest = ModuleManifest.from_dict(bundled_manifest("brands"))
    core = api.runtime.modules
    core.report_node("text-node", [{**manifest.to_dict(), "ready": True,
                                    "endpoint": f"http://127.0.0.1:{server.server_address[1]}"}])
    core.set_placement("brands", ["text-node"])
    legacy = tmp_path / "legacy-brands"
    monkeypatch.setenv("BRANDS_ROOT", str(legacy))
    yield {"api": api, "server": server, "connection": connection, "legacy": legacy, "lifecycle": lifecycle}
    server.shutdown()
    connection.close()


def _rows(brands, sql):
    cursor = brands["connection"].cursor()
    cursor.execute(sql)
    return [list(row) for row in cursor.fetchall()]


def test_core_brand_and_channel_api_use_the_module_and_shared_channels_are_linked(brands):
    from core.brand_store import list_channels
    from core.publishing_targets import brand_targets
    api = brands["api"]
    for brand in ("brand-a", "brand-b"):
        created = api.post("/api/brands", json={"id": brand, "name": f"Бренд {brand[-1].upper()}"})
        assert created.status_code == 200, created.text
    assert not brands["legacy"].exists()  # nothing written to the legacy store
    assert [b["id"] for b in api.get("/api/brands").json()] == ["brand-a", "brand-b"]
    assert _rows(brands, "SELECT id, publication_policy FROM brands ORDER BY id") == [
        ["brand-a", "approval_required"], ["brand-b", "approval_required"]]
    channel = api.post("/api/brands/brand-a/channels",
                       json={"brand_id": "brand-a", "channel_type": "telegram", "target": "@shared_news"})
    assert channel.status_code == 200, channel.text
    channel_id = channel.json()["channel_id"]
    # Brand B joins the same physical channel: a link, not a second connection.
    from core.module.gateway import call_module
    status, linked = call_module("brands", "PUT", f"/brands/brand-b/channels/{channel_id}", None, {"policy": "automatic"})
    assert status == 200 and linked["brands"] == ["brand-a", "brand-b"]
    assert _rows(brands, "SELECT count(*) FROM channels") == [[1]]
    assert brand_targets("brand-a", "telegram") == brand_targets("brand-b", "telegram") == ["@shared_news"]
    assert {c.brand_id for c in list_channels()} == {"brand-a", "brand-b"}
    # Policy: the channel override wins; "inherit" follows the brand.
    assert call_module("brands", "GET", f"/brands/brand-b/channels/{channel_id}/policy")[1]["policy"] == "automatic"
    assert call_module("brands", "GET", f"/brands/brand-a/channels/{channel_id}/policy")[1]["policy"] == "approval_required"
    api.put("/api/brands/brand-a", json={"id": "brand-a", "name": "Бренд A", "publication_policy": "automatic"})
    assert call_module("brands", "GET", f"/brands/brand-a/channels/{channel_id}/policy")[1]["policy"] == "automatic"
    assert api.delete(f"/api/channels/{channel_id}").status_code == 200
    assert brand_targets("brand-b", "telegram") == []
    audit = [row[0] for row in _rows(brands, "SELECT action FROM brand_audit ORDER BY id")]
    assert audit[:4] == ["create", "create", "channel_create", "channel_link"] and "channel_delete" in audit


def test_legacy_brands_and_channels_migrate_with_their_ids(brands):
    from core.models import Channel
    from core.state import store
    legacy = brands["legacy"] / "old-brand"
    legacy.mkdir(parents=True)
    (legacy / "brand.json").write_text(json.dumps({"id": "old-brand", "name": "Старий бренд",
                                                   "metadata": {"watermark": "w.png"}}))
    store.repository.save_channel(Channel(channel_id="ch-legacy01", brand_id="old-brand", channel_type="youtube",
                                          target="UC-old"))
    try:
        result = brands["api"].post("/api/brands/migrate-to-module")
        assert result.status_code == 200, result.text
        assert result.json()["brands"] == ["old-brand"] and result.json()["channels"] == ["ch-legacy01"]
        again = brands["api"].post("/api/brands/migrate-to-module").json()  # idempotent
        assert again["brands"] == [] and again["channels"] == []
        assert brands["api"].get("/api/brands/old-brand/channels").json()[0]["channel_id"] == "ch-legacy01"
        from core.brand_store import get_brand
        assert get_brand("old-brand")["metadata"] == {"watermark": "w.png"}  # used by assembly/storyboard
        assert (legacy / "brand.json").is_file()  # the legacy store is kept, not deleted
    finally:
        store.repository.delete_channel("ch-legacy01")


def test_moderators_change_only_their_brands_and_a_silent_module_fails_closed(brands):
    import http.client
    import brands_module
    api = brands["api"]
    api.post("/api/brands", json={"id": "brand-a", "name": "A"})
    api.post("/api/brands", json={"id": "brand-b", "name": "B"})

    def gateway(method, path, role, user_brands, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", brands["server"].server_address[1], timeout=10)
        connection.request(method, path, json.dumps(body).encode() if body is not None else None,
                           {"x-vertep-module-token": brands_module.TOKEN, "x-vertep-role": role,
                            "x-vertep-brands": user_brands, "x-vertep-user": "mod", "content-type": "application/json"})
        response = connection.getresponse()
        status = response.status
        response.read()
        connection.close()
        return status

    assert gateway("PUT", "/brands/brand-a", "moderator", "brand-a", {"name": "A2"}) == 200
    assert gateway("PUT", "/brands/brand-b", "moderator", "brand-a", {"name": "B2"}) == 403
    assert gateway("POST", "/brands", "moderator", "brand-a", {"id": "brand-c", "name": "C"}) == 403
    assert gateway("DELETE", "/brands/brand-a", "moderator", "brand-a") == 403
    assert gateway("POST", "/migrate", "moderator", "brand-a", {}) == 403
    assert gateway("GET", "/brands", "moderator", "") == 200
    # The user request never passes as a CORE-internal call.
    assert gateway("POST", "/brands", "", "", {"id": "brand-x", "name": "X"}) == 403
    brands["server"].shutdown()
    brands["server"].server_close()
    assert api.get("/api/brands").status_code == 503  # no silent fallback to stale legacy data
