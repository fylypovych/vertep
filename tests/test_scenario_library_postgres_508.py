"""#508 Real PostgreSQL acceptance test: Storyboard Scenario Library migrations,
isolated schema ``mod_storyboard``, pg8000 CRUD, modular export and backup/restore.
"""

from __future__ import annotations

import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import pg8000.dbapi
import pytest

from core.module.data import ModuleDatabase
from core.module.instances import ModuleInstanceRegistry, PlacementStore
from core.module.lifecycle import ModuleLifecycle, bundled_manifest, migration_sources
from core.module.manifest import ModuleManifest
from tests.module_postgres import module_postgres  # noqa: F401  (fixture)
from tests.test_characters_module import api  # noqa: F401  (fixture)

MODULE_DIR = Path(__file__).resolve().parents[1] / "modules" / "storyboard"


def test_scenario_library_postgres_migrations_export_and_restore(api, tmp_path, monkeypatch, module_postgres):  # noqa: F811
    from core.module.gateway import issue_token

    if str(MODULE_DIR) not in sys.path:
        sys.path.insert(0, str(MODULE_DIR))
    import storyboard_module

    db = ModuleDatabase(module_postgres)
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.4.16"}, PlacementStore(tmp_path / "placement.json"))
    credentials: dict[str, dict] = {}
    lifecycle = ModuleLifecycle(
        tmp_path / "lc",
        lambda package: None,
        database=db,
        registry=registry,
        system_versions={"core_runtime": "0.0.4.16"},
        store_credentials=lambda module_id, value: credentials.__setitem__(module_id, value),
    )

    assert lifecycle.install_bundled("storyboard")["state"] == "INSTALLED"
    registry.approve_permissions("storyboard", ["storyboard.read"])
    assert lifecycle.activate("storyboard")["state"] == "ACTIVE"
    assert db.exists("storyboard")
    assert [m["id"] for m in db.applied("storyboard")] == ["m001_initial"]

    parsed = urlparse(module_postgres)

    def open_module_conn():
        conn = pg8000.dbapi.connect(
            user=credentials["storyboard"]["role"],
            password=credentials["storyboard"]["password"],
            host=parsed.hostname,
            port=parsed.port,
            database=unquote(parsed.path.lstrip("/")),
        )
        conn.autocommit = True
        return conn

    conn = open_module_conn()
    monkeypatch.setattr(storyboard_module, "TOKEN", issue_token("storyboard", "app-node"))
    monkeypatch.setattr(storyboard_module, "_state", {"connection": conn, "store": None})
    server = ThreadingHTTPServer(("127.0.0.1", 0), storyboard_module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
    core_reg = api.runtime.modules
    core_reg.report_node("app-node", [{
        **manifest.to_dict(), "ready": True, "endpoint": f"http://127.0.0.1:{server.server_address[1]}",
    }])
    core_reg.set_placement("storyboard", ["app-node"])
    core_reg.approve_permissions("storyboard", list(manifest.permissions))

    try:
        base = "/api/modules/storyboard/v1"
        created = api.post(f"{base}/scenarios", json={
            "owner_brand_id": "brand-a",
            "title": "PostgreSQL сценарій",
            "description": "Перевірка експорту та відновлення",
            "scenes": [
                {"prompt": "Перша сцена", "location": "Студія", "dialogue": "Привіт!",
                 "characters": ["did_samogon"], "duration": 5},
            ],
        })
        assert created.status_code == 201, created.text
        sid = created.json()["scenario_id"]
        v1 = api.post(f"{base}/scenarios/{sid}/versions", json={"note": "v1"})
        assert v1.status_code == 201
        assert api.post(f"{base}/scenarios/{sid}/shares", json={"brand_id": "brand-b"}).status_code == 200
        copy = api.post(f"{base}/scenarios/{sid}/copy", json={"target_brand_id": "brand-b", "version": 1})
        assert copy.status_code == 201
        copy_id = copy.json()["scenario_id"]

        before_detail = api.get(f"{base}/scenarios/{sid}").json()
        before_audit = api.get(f"{base}/scenarios/{sid}/audit").json()
        exported = db.export("storyboard")
        assert set(exported["tables"]) >= {"scenarios", "scenario_versions", "scenario_shares", "scenario_audit"}

        # Damage/drop schema and restore cleanly through ModuleDatabase.restore().
        conn.close()
        record = lifecycle.record("storyboard")
        sources = migration_sources(record["package"], manifest)
        db.drop_schema("mod_storyboard", "storyboard")
        assert not db.exists("storyboard")
        db.restore("storyboard", exported, sources)
        assert db.exists("storyboard")

        # Reconnect as module role and verify full fidelity + working sequences after restore.
        conn = open_module_conn()
        monkeypatch.setattr(storyboard_module, "_state", {"connection": conn, "store": None})
        after_detail = api.get(f"{base}/scenarios/{sid}").json()
        after_audit = api.get(f"{base}/scenarios/{sid}/audit").json()
        assert after_detail["title"] == before_detail["title"]
        assert after_detail["versions"] == before_detail["versions"]
        assert after_detail["shared_brands"] == ["brand-b"]
        assert len(after_audit) == len(before_audit)
        assert api.get(f"{base}/scenarios/{copy_id}").json()["copied_from_scenario_id"] == sid

        # New writes and serial audit IDs continue cleanly after restore.
        v2_draft = api.put(f"{base}/scenarios/{sid}", json={"title": "Після відновлення"})
        assert v2_draft.status_code == 200
        assert len(api.get(f"{base}/scenarios/{sid}/audit").json()) == len(after_audit) + 1
    finally:
        server.shutdown()
        try:
            conn.close()
        except Exception:
            pass
