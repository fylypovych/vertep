"""#508 Scenario Library for Storyboard Module — end-to-end API, CORE integration,
concurrency idempotency, RBAC, independence and Browser UI acceptance tests.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from core.module.instances import ModuleInstanceRegistry, PlacementStore
from core.module.lifecycle import bundled_manifest
from core.module.manifest import ModuleManifest
from tests.test_characters_module import api  # noqa: F401  (fixture)
from tests.test_media_browser_e2e import browser, core, expect  # noqa: F401  (fixtures)

MODULE_DIR = Path(__file__).resolve().parents[1] / "modules" / "storyboard"
MIGRATION_SQL = MODULE_DIR / "migrations" / "m001_initial.sql"


class _SqlitePgCursor:
    """Minimal PostgreSQL-to-SQLite cursor adapter for executing m001_initial.sql
    and ScenarioLibraryStore SQL in environments without a local PostgreSQL server."""

    def __init__(self, inner: sqlite3.Cursor) -> None:
        self._inner = inner

    @property
    def description(self):
        return self._inner.description

    def execute(self, sql: str, params=()):
        translated = sql.replace("::jsonb", "").replace("NOW()", "CURRENT_TIMESTAMP")
        translated = translated.replace("%s", "?")
        self._inner.execute(translated, tuple(params) if params else ())
        return self

    def fetchone(self):
        return self._inner.fetchone()

    def fetchall(self):
        return self._inner.fetchall()

    def close(self) -> None:
        self._inner.close()


class _SqlitePgConnection:
    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        ddl = MIGRATION_SQL.read_text(encoding="utf-8")
        ddl = re.sub(r"\bBIGSERIAL\b", "INTEGER", ddl)
        ddl = re.sub(r"\bTIMESTAMPTZ\b", "TEXT", ddl)
        ddl = re.sub(r"\bJSONB\b", "TEXT", ddl)
        ddl = ddl.replace("'[]'::jsonb", "'[]'").replace("'{}'::jsonb", "'{}'")
        ddl = ddl.replace("NOW()", "CURRENT_TIMESTAMP")
        self._conn.executescript(ddl)

    def cursor(self) -> _SqlitePgCursor:
        return _SqlitePgCursor(self._conn.cursor())

    def close(self) -> None:
        self._conn.close()


def make_sqlite_scenario_store():
    import sys
    if str(MODULE_DIR) not in sys.path:
        sys.path.insert(0, str(MODULE_DIR))
    from storyboard_logic import ScenarioLibraryStore
    conn = _SqlitePgConnection()
    return ScenarioLibraryStore(lambda: conn)


def _mod_headers(user: str, role: str, brands: list[str], extra: dict | None = None) -> dict:
    from core.first_run import password_hash
    # Helper used with Basic auth headers after creating the user in USERS_JSON.
    import base64
    raw = f"{user}:{user}-password-12".encode()
    headers = {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    if extra:
        headers.update(extra)
    return headers


@pytest.fixture
def library_env(api, tmp_path, monkeypatch):
    """Real CORE FastAPI TestClient + real Storyboard HTTP server + ScenarioLibraryStore."""
    import sys
    from core.first_run import password_hash
    from core.module.gateway import issue_token
    if str(MODULE_DIR) not in sys.path:
        sys.path.insert(0, str(MODULE_DIR))
    import storyboard_module

    users = {
        "mod-a": {"password_hash": password_hash("mod-a-password-12"), "role": "moderator", "brands": ["brand-a"]},
        "mod-b": {"password_hash": password_hash("mod-b-password-12"), "role": "moderator", "brands": ["brand-b"]},
        "mod-bc": {"password_hash": password_hash("mod-bc-password-12"), "role": "moderator", "brands": ["brand-b", "brand-c"]},
    }
    monkeypatch.setenv("USERS_JSON", json.dumps(users))

    # Ensure a valid complete character exists in CHARACTERS_ROOT for Job creation tests.
    import os
    chars_root = Path(os.environ["CHARACTERS_ROOT"])
    char_dir = chars_root / "did_samogon"
    char_dir.mkdir(parents=True, exist_ok=True)
    (char_dir / "character.json").write_text(json.dumps({"id": "did_samogon", "name": "Дід Самогонщик"}), encoding="utf-8")
    (char_dir / "system_prompt.txt").write_text("Тестовий персонаж", encoding="utf-8")
    (char_dir / "voice.json").write_text(json.dumps({"provider": "disabled"}), encoding="utf-8")
    (char_dir / "visual.json").write_text(json.dumps({"aspect_ratio": "16:9", "output_preset": "youtube"}), encoding="utf-8")
    (char_dir / "generation.json").write_text(json.dumps({"workflow": "workflows/image/demo.json", "min_vram_mb": 0}), encoding="utf-8")
    (char_dir / "publishing.json").write_text(json.dumps({}), encoding="utf-8")

    store_obj = make_sqlite_scenario_store()
    monkeypatch.setattr(storyboard_module, "TOKEN", issue_token("storyboard", "app-node"))
    monkeypatch.setattr(storyboard_module, "_state", {"connection": None, "store": store_obj})

    def core_get(path):
        r = api.get(path, headers={"x-vertep-module-token": storyboard_module.TOKEN, "Authorization": ""})
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise storyboard_module.CoreUnavailable(str(r.status_code))
        return r.json()

    def core_post(path, body, user_context=""):
        headers = {"x-vertep-module-token": storyboard_module.TOKEN, "Authorization": ""}
        if user_context:
            headers["x-vertep-user-context"] = user_context
        r = api.post(path, json=body, headers=headers)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"detail": r.text}

    monkeypatch.setattr(storyboard_module, "_core", core_get)
    monkeypatch.setattr(storyboard_module, "_core_post", core_post)

    server = ThreadingHTTPServer(("127.0.0.1", 0), storyboard_module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
    registry = api.runtime.modules
    registry.report_node("app-node", [{
        **manifest.to_dict(),
        "ready": True,
        "endpoint": f"http://127.0.0.1:{server.server_address[1]}",
    }])
    registry.set_placement("storyboard", ["app-node"])
    registry.approve_permissions("storyboard", list(manifest.permissions))

    yield {
        "api": api,
        "server": server,
        "registry": registry,
        "store": store_obj,
        "module": storyboard_module,
        "mod_a": _mod_headers("mod-a", "moderator", ["brand-a"]),
        "mod_b": _mod_headers("mod-b", "moderator", ["brand-b"]),
        "mod_bc": _mod_headers("mod-bc", "moderator", ["brand-b", "brand-c"]),
    }
    server.shutdown()


def test_draft_editing_immutable_versions_catalog_and_archive(library_env):
    api = library_env["api"]
    mod_a = library_env["mod_a"]
    base = "/api/modules/storyboard/v1"

    # Create scenario before any Job exists.
    created = api.post(f"{base}/scenarios", headers=mod_a, json={
        "owner_brand_id": "brand-a",
        "title": "Ранкова кава",
        "description": "Початковий опис",
        "scenes": [
            {"prompt": "Кухня на світанку", "location": "Кухня", "dialogue": "Доброго ранку!",
             "characters": ["did_samogon"], "duration": 4},
            {"prompt": "Чашка парує на столі", "location": "Стіл", "dialogue": "Свіжа кава.",
             "characters": ["did_samogon"], "duration": 3},
        ],
    })
    assert created.status_code == 201, created.text
    sid = created.json()["scenario_id"]
    assert created.json()["latest_version"] == 0
    assert created.json()["relation"] == "own"

    # Save immutable version 1.
    v1 = api.post(f"{base}/scenarios/{sid}/versions", headers=mod_a, json={"note": "Перша редакція"})
    assert v1.status_code == 201, v1.text
    v1_body = v1.json()
    assert v1_body["version"] == 1
    assert [s["prompt"] for s in v1_body["scenes"]] == ["Кухня на світанку", "Чашка парує на столі"]
    v1_digest = v1_body["snapshot_sha256"]

    # Edit draft (reorder + change text) and save version 2.
    updated = api.put(f"{base}/scenarios/{sid}", headers=mod_a, json={
        "title": "Ранкова кава v2",
        "scenes": [
            {"prompt": "Сад після дощу", "location": "Сад", "dialogue": "Свіже повітря",
             "characters": ["did_samogon"], "duration": 6},
        ],
    })
    assert updated.status_code == 200, updated.text
    v2 = api.post(f"{base}/scenarios/{sid}/versions", headers=mod_a, json={"note": "Друга редакція"})
    assert v2.status_code == 201 and v2.json()["version"] == 2

    # Version 1 MUST remain completely immutable!
    v1_again = api.get(f"{base}/scenarios/{sid}/versions/1", headers=mod_a)
    assert v1_again.status_code == 200
    assert v1_again.json()["title"] == "Ранкова кава"
    assert [s["prompt"] for s in v1_again.json()["scenes"]] == ["Кухня на світанку", "Чашка парує на столі"]
    assert v1_again.json()["snapshot_sha256"] == v1_digest

    # History shows both versions; search & filter work.
    history = api.get(f"{base}/scenarios/{sid}/versions", headers=mod_a).json()
    assert [h["version"] for h in history] == [2, 1]
    searched = api.get(f"{base}/scenarios", params={"q": "дощу"}, headers=mod_a).json()
    assert [item["scenario_id"] for item in searched] == [sid]

    # Archive blocks editing and version saving until restored.
    archived = api.post(f"{base}/scenarios/{sid}/archive", headers=mod_a)
    assert archived.status_code == 200 and archived.json()["archived"] is True
    assert api.get(f"{base}/scenarios", headers=mod_a).json() == []
    assert [x["scenario_id"] for x in api.get(f"{base}/scenarios", params={"archived": "true"}, headers=mod_a).json()] == [sid]
    assert api.put(f"{base}/scenarios/{sid}", headers=mod_a, json={"title": "Blocked"}).status_code == 409
    assert api.post(f"{base}/scenarios/{sid}/versions", headers=mod_a, json={}).status_code == 409

    restored = api.post(f"{base}/scenarios/{sid}/restore", headers=mod_a)
    assert restored.status_code == 200 and restored.json()["archived"] is False


def test_inter_brand_sharing_rbac_copy_independence_revocation_and_audit(library_env):
    api = library_env["api"]
    mod_a, mod_b, mod_bc = library_env["mod_a"], library_env["mod_b"], library_env["mod_bc"]
    base = "/api/modules/storyboard/v1"

    sid = api.post(f"{base}/scenarios", headers=mod_a, json={
        "owner_brand_id": "brand-a",
        "title": "Спільний сюжет",
        "scenes": [{"prompt": "Оригінальна сцена", "characters": ["did_samogon"], "duration": 5}],
    }).json()["scenario_id"]
    assert api.post(f"{base}/scenarios/{sid}/versions", headers=mod_a, json={"note": "v1"}).status_code == 201

    # Private by default: brand-b cannot see or mutate it.
    assert api.get(f"{base}/scenarios", headers=mod_b).json() == []
    assert api.get(f"{base}/scenarios/{sid}", headers=mod_b).status_code == 404
    assert api.put(f"{base}/scenarios/{sid}", headers=mod_b, json={"title": "Hack"}).status_code == 403
    assert api.post(f"{base}/scenarios/{sid}/shares", headers=mod_b, json={"brand_id": "brand-b"}).status_code == 403

    # Owner shares with brand-b.
    granted = api.post(f"{base}/scenarios/{sid}/shares", headers=mod_a, json={"brand_id": "brand-b"})
    assert granted.status_code == 200
    assert [s["brand_id"] for s in granted.json()["shares"]] == ["brand-b"]

    # brand-b sees it as shared, cannot edit/archive/manage shares, cannot copy into unshared brand-c.
    listed_b = api.get(f"{base}/scenarios", params={"scope": "shared"}, headers=mod_b).json()
    assert len(listed_b) == 1 and listed_b[0]["scenario_id"] == sid
    assert listed_b[0]["relation"] == "shared"
    assert listed_b[0]["can_edit"] is False
    assert listed_b[0]["can_manage_access"] is False
    assert listed_b[0]["can_archive"] is False
    assert listed_b[0]["can_copy"] is True
    assert listed_b[0]["can_create_job"] is True
    assert api.put(f"{base}/scenarios/{sid}", headers=mod_b, json={"title": "Forbidden"}).status_code == 403
    assert api.post(f"{base}/scenarios/{sid}/archive", headers=mod_b).status_code == 403
    assert api.post(f"{base}/scenarios/{sid}/versions", headers=mod_b, json={}).status_code == 403
    assert api.post(f"{base}/scenarios/{sid}/copy", headers=mod_bc,
                    json={"target_brand_id": "brand-c", "version": 1}).status_code == 403

    # brand-b copies version 1 into its own independent scenario and edits its copy.
    copied = api.post(f"{base}/scenarios/{sid}/copy", headers=mod_b,
                      json={"target_brand_id": "brand-b", "version": 1, "title": "Копія бренду B"})
    assert copied.status_code == 201, copied.text
    copy_id = copied.json()["scenario_id"]
    assert copy_id != sid
    assert copied.json()["owner_brand_id"] == "brand-b"
    assert copied.json()["copied_from_scenario_id"] == sid
    assert copied.json()["copied_from_version"] == 1
    assert copied.json()["relation"] == "own" and copied.json()["can_edit"] is True

    edited_copy = api.put(f"{base}/scenarios/{copy_id}", headers=mod_b, json={
        "title": "Змінена копія B",
        "scenes": [{"prompt": "Власна сцена B", "characters": ["did_samogon"], "duration": 7}],
    })
    assert edited_copy.status_code == 200
    # Original is untouched by edits to the copy.
    assert api.get(f"{base}/scenarios/{sid}/versions/1", headers=mod_a).json()["scenes"][0]["prompt"] == "Оригінальна сцена"

    # brand-b creates a Job from the shared original BEFORE revocation.
    job_resp = api.post(f"{base}/scenarios/{sid}/jobs", headers=mod_b, json={
        "version": 1, "brand_id": "brand-b", "idempotency_key": "shared-job-1",
    })
    assert job_resp.status_code == 200, job_resp.text
    shared_job_id = job_resp.json()["job_id"]

    # Owner revokes brand-b access -> original is immediately closed for brand-b,
    # while brand-b's copy AND already-created Job stay available!
    revoked = api.delete(f"{base}/scenarios/{sid}/shares/brand-b", headers=mod_a)
    assert revoked.status_code == 200 and revoked.json()["shares"] == []
    assert api.get(f"{base}/scenarios/{sid}", headers=mod_b).status_code == 404
    assert api.post(f"{base}/scenarios/{sid}/jobs", headers=mod_b, json={
        "version": 1, "brand_id": "brand-b", "idempotency_key": "after-revoke",
    }).status_code == 404
    assert api.get(f"{base}/scenarios/{copy_id}", headers=mod_b).status_code == 200
    assert api.get(f"/api/jobs/{shared_job_id}", headers=mod_b).status_code == 200

    # Access changes are audited.
    audit = api.get(f"{base}/scenarios/{sid}/audit", headers=mod_a).json()
    actions = [entry["action"] for entry in audit]
    assert "share_grant" in actions and "share_revoke" in actions


def test_job_creation_character_checks_concurrent_idempotency_and_independence(library_env, monkeypatch):
    from core.state import store
    api = library_env["api"]
    mod_a, mod_b = library_env["mod_a"], library_env["mod_b"]
    base = "/api/modules/storyboard/v1"

    sid = api.post(f"{base}/scenarios", headers=mod_a, json={
        "owner_brand_id": "brand-a",
        "title": "Ідемпотентний сценарій",
        "scenes": [
            {"prompt": "Кадр перший", "dialogue": "Текст один", "characters": ["did_samogon"], "duration": 4},
            {"prompt": "Кадр другий", "dialogue": "Текст два", "characters": ["did_samogon"], "duration": 6},
        ],
    }).json()["scenario_id"]
    v1 = api.post(f"{base}/scenarios/{sid}/versions", headers=mod_a, json={"note": "v1"}).json()

    # Moderator of brand-b cannot create a Job for brand-a (403) or unshared scenario (404).
    assert api.post("/api/jobs/from-scenario", headers=mod_b, json={
        "scenario_id": sid, "version": 1, "brand_id": "brand-a",
    }).status_code == 403
    assert api.post("/api/jobs/from-scenario", headers=mod_b, json={
        "scenario_id": sid, "version": 1, "brand_id": "brand-b",
    }).status_code == 404

    # Character access check via Characters module is enforced (refused -> 422).
    monkeypatch.setattr("core.api.jobs._check_character_module",
                        lambda cid, bid: (_ for _ in ()).throw(__import__("fastapi").HTTPException(422, "character denied")))
    refused = api.post("/api/jobs/from-scenario", headers=mod_a, json={
        "scenario_id": sid, "version": 1, "brand_id": "brand-a",
    })
    assert refused.status_code == 422

    # Allow character owned by brand-c -> CORE records character_owner_brands provenance.
    monkeypatch.setattr("core.api.jobs._check_character_module", lambda cid, bid: "brand-c")

    # Concurrent duplicate retries with the SAME idempotency_key produce EXACTLY ONE Job.
    before_count = len(store.jobs)

    def send_create():
        return api.post("/api/jobs/from-scenario", headers=mod_a, json={
            "scenario_id": sid, "version": 1, "brand_id": "brand-a", "idempotency_key": "retry-key-42",
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: send_create(), range(6)))

    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    job_ids = {r.json()["job_id"] for r in responses}
    assert len(job_ids) == 1
    assert len(store.jobs) == before_count + 1
    job1_id = next(iter(job_ids))
    job1 = responses[0].json()
    assert job1["scenario_source"]["scenario_id"] == sid
    assert job1["scenario_source"]["version"] == 1
    assert job1["scenario_source"]["snapshot_sha256"] == v1["snapshot_sha256"]
    assert job1["character_owner_brands"] == ["brand-c"]
    assert job1["status"] == "STORYBOARD_PENDING_APPROVAL"
    assert [s["prompt"] for s in job1["storyboards"][0]["scenes"]] == ["Кадр перший", "Кадр другий"]

    # Separate action (new idempotency_key) from the SAME version creates a second independent Job.
    second = api.post(f"{base}/scenarios/{sid}/jobs", headers=mod_a, json={
        "version": 1, "brand_id": "brand-a", "idempotency_key": "separate-action-2",
    })
    assert second.status_code == 200, second.text
    job2_id = second.json()["job_id"]
    assert job2_id != job1_id
    assert len(store.jobs) == before_count + 2

    # Now mutate original scenario, save v2, archive it, AND remove Storyboard module completely!
    assert api.put(f"{base}/scenarios/{sid}", headers=mod_a, json={
        "title": "Переписаний оригінал",
        "scenes": [{"prompt": "Зовсім інша сцена", "characters": ["did_samogon"], "duration": 9}],
    }).status_code == 200
    assert api.post(f"{base}/scenarios/{sid}/versions", headers=mod_a, json={"note": "v2"}).status_code == 201
    assert api.post(f"{base}/scenarios/{sid}/archive", headers=mod_a).status_code == 200
    # Archived scenario cannot create new Jobs.
    assert api.post(f"{base}/scenarios/{sid}/jobs", headers=mod_a, json={
        "version": 1, "brand_id": "brand-a",
    }).status_code == 409

    # Shut down and unregister Storyboard module: existing Jobs keep working with their own snapshot!
    library_env["server"].shutdown()
    library_env["registry"].remove_node("app-node")

    boards = api.get(f"/api/jobs/{job1_id}/storyboards").json()
    assert len(boards) == 1
    assert boards[0]["title"] == "Ідемпотентний сценарій"
    assert [s["prompt"] for s in boards[0]["scenes"]] == ["Кадр перший", "Кадр другий"]
    # Existing CORE preview -> images/approve -> storyboards/approve flow keeps working
    # completely independently while Storyboard module is down.
    from core.image_storyboard import handle_image_result
    # Valid 1x1 PNG payload (media_integrity verifies PNG signature + IHDR/IDAT/IEND).
    b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    job1_obj = store.jobs[job1_id]
    assert len(job1_obj.image_storyboard_task_ids) == 2
    for tid in list(job1_obj.image_storyboard_task_ids.keys()):
        handle_image_result(store, job1_obj, tid, True, image_base64=b64)
    img_ok = api.post(f"/api/jobs/{job1_id}/storyboards/images/approve", json={"version": 1, "actor": "editor"})
    assert img_ok.status_code == 200, img_ok.text
    approved = api.post(f"/api/jobs/{job1_id}/storyboards/approve", json={"version": 1, "actor": "editor"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["storyboards"][0]["status"] == "approved"


def test_scenario_library_ui_rbac_and_flow_in_browser(core, browser, monkeypatch, tmp_path):  # noqa: F811
    """Browser E2E (#508): Owner creates scenario, saves version, shares with brand-b,
    creates Job from UI; brand-b moderator sees 'Спільний' with only 'Створити Job' and
    'Створити копію' (no edit/archive/share), then copies into its own brand."""
    import sys
    from core.first_run import password_hash
    from core.module.gateway import issue_token

    if str(MODULE_DIR) not in sys.path:
        sys.path.insert(0, str(MODULE_DIR))
    import storyboard_module

    # Register a moderator for brand-b in the live CORE instance.
    mod_pass = "mod-b-browser-pass-12"
    core["monkeypatch"].setenv("USERS_JSON", json.dumps({
        "mod-b": {"password_hash": password_hash(mod_pass), "role": "moderator", "brands": ["brand-b"]},
    }))
    monkeypatch.setenv("VERTEP_MODULE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.4.16"}, PlacementStore(tmp_path / "placement.json"))
    monkeypatch.setattr("core.module.gateway._modules_or_none", lambda: registry)
    monkeypatch.setattr(storyboard_module, "CORE", core["url"])
    monkeypatch.setattr(storyboard_module, "TOKEN", issue_token("storyboard", "app-node"))
    monkeypatch.setattr(storyboard_module, "_state", {"connection": None, "store": make_sqlite_scenario_store()})
    server = ThreadingHTTPServer(("127.0.0.1", 0), storyboard_module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
    registry.report_node("app-node", [{
        **manifest.to_dict(), "ready": True, "endpoint": f"http://127.0.0.1:{server.server_address[1]}",
    }])
    registry.set_placement("storyboard", ["app-node"])
    registry.approve_permissions("storyboard", list(manifest.permissions))

    page = browser["page"]
    try:
        page.goto(f"{browser['url']}/modules/storyboard")
        frame = page.frame_locator("[data-testid=module-extension-frame]")
        expect(frame.get_by_test_id("scenario-library")).to_be_visible()

        # Admin creates a scenario for brand-a, saves version 1, shares with brand-b, creates Job.
        frame.get_by_test_id("scenario-new-btn").click()
        frame.get_by_test_id("editor-brand").fill("brand-a")
        frame.get_by_test_id("editor-title").fill("Сценарій із браузера")
        frame.locator("[data-field=prompt]").first.fill("Зимовий вечір у селі")
        frame.locator("[data-field=characters]").first.fill(core["character_id"])
        frame.get_by_test_id("editor-save-btn").click()

        expect(frame.get_by_test_id("scenario-detail")).to_contain_text("Сценарій із браузера")
        expect(frame.get_by_test_id("detail-relation-badge")).to_have_text("Власний")
        frame.get_by_test_id("version-note-input").fill("Готово до виробництва")
        frame.get_by_test_id("action-save-version").click()
        expect(frame.get_by_test_id("version-history")).to_contain_text("v1")

        # Share with brand-b.
        frame.get_by_test_id("grant-share-brand").fill("brand-b")
        frame.get_by_test_id("grant-share-btn").click()
        expect(frame.get_by_test_id("revoke-share-brand-b")).to_be_visible()

        # Create Job from UI.
        frame.get_by_test_id("job-target-brand").fill("brand-a")
        frame.get_by_test_id("action-create-job").click()
        expect(frame.get_by_test_id("scenario-status")).to_contain_text("Створено Job")

        # Switch browser session to moderator of brand-b.
        import base64
        auth = "Basic " + base64.b64encode(f"mod-b:{mod_pass}".encode()).decode()
        logout = page.request.delete(f"{browser['url']}/api/session")
        assert logout.ok
        login = page.request.post(f"{browser['url']}/api/session", headers={"Authorization": auth})
        assert login.ok, login.text()

        page.goto(f"{browser['url']}/modules/storyboard")
        frame = page.frame_locator("[data-testid=module-extension-frame]")
        expect(frame.get_by_test_id("scenario-list")).to_contain_text("Сценарій із браузера")
        expect(frame.get_by_test_id("scenario-list")).to_contain_text("Спільний")
        frame.locator("[data-open-scenario]").first.click()

        expect(frame.get_by_test_id("detail-relation-badge")).to_have_text("Спільний")
        expect(frame.get_by_test_id("action-create-job")).to_be_visible()
        expect(frame.get_by_test_id("action-copy-scenario")).to_be_visible()
        # Shared brand MUST NOT see owner-only controls!
        expect(frame.get_by_test_id("action-edit-draft")).to_have_count(0)
        expect(frame.get_by_test_id("action-archive")).to_have_count(0)
        expect(frame.get_by_test_id("shares-panel")).to_have_count(0)

        # brand-b creates its own copy and immediately owns it.
        frame.get_by_test_id("copy-target-brand").fill("brand-b")
        frame.get_by_test_id("action-copy-scenario").click()
        expect(frame.get_by_test_id("detail-relation-badge")).to_have_text("Власний")
        expect(frame.get_by_test_id("action-edit-draft")).to_be_visible()
        expect(frame.get_by_test_id("shares-panel")).to_be_visible()
    finally:
        server.shutdown()
        # Switching to a moderator session triggers expected 403 on admin-only sidebar probes
        # (/api/settings/lab-panel, /api/nodes/module-catalog); Scenario Library calls must have zero errors.
        browser["console_errors"][:] = [m for m in browser["console_errors"] if "403 (Forbidden)" not in m]


def test_scenario_library_lifecycle_export_and_restore(tmp_path):
    """#508: Scenario Library data survives modular lifecycle snapshot/export,
    schema loss and restore (also verified on real PostgreSQL in test_scenario_library_postgres_508.py)."""
    import csv
    import io
    import sys
    from core.module.lifecycle import ModuleLifecycle, migration_sources

    if str(MODULE_DIR) not in sys.path:
        sys.path.insert(0, str(MODULE_DIR))
    from storyboard_logic import ScenarioLibraryStore

    class _ExportableSqliteModuleDatabase:
        """Simulates ModuleDatabase CSV export/restore using the real m001_initial.sql."""

        def __init__(self) -> None:
            self.conn: _SqlitePgConnection | None = None
            self.applied_migrations: list[dict] = []

        def exists(self, module_id: str) -> bool:
            return self.conn is not None

        def ensure(self, module_id: str) -> dict:
            if self.conn is None:
                self.conn = _SqlitePgConnection()
            return {"role": f"mod_{module_id}", "schema": f"mod_{module_id}", "password": "pw", "database": "vertep"}

        def migrate(self, module_id: str, migrations: list[dict], sources: dict[str, str]) -> list[str]:
            for item in migrations:
                assert item["id"] in sources and "CREATE TABLE IF NOT EXISTS scenarios" in sources[item["id"]]
            self.applied_migrations = list(migrations)
            return [m["id"] for m in migrations]

        def export(self, module_id: str) -> dict:
            tables = {}
            for table in ("scenarios", "scenario_versions", "scenario_shares", "scenario_audit"):
                cur = self.conn._conn.execute(f"SELECT * FROM {table}")
                cols = [d[0] for d in cur.description]
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(cols)
                for row in cur.fetchall():
                    writer.writerow(["" if v is None else v for v in row])
                tables[table] = buf.getvalue()
            return {"schema": f"mod_{module_id}", "migrations": list(self.applied_migrations), "tables": tables}

        def restore(self, module_id: str, exported: dict, sources: dict[str, str]) -> None:
            self.conn = _SqlitePgConnection()
            self.migrate(module_id, exported["migrations"], sources)
            for table in ("scenarios", "scenario_versions", "scenario_shares", "scenario_audit"):
                rows = list(csv.reader(io.StringIO(exported["tables"][table])))
                header, body = rows[0], rows[1:]
                placeholders = ", ".join(["?"] * len(header))
                cols = ", ".join(header)
                for r in body:
                    vals = [None if v == "" and header[i] in {"archived_at", "archived_by", "copied_from_scenario_id", "copied_from_version", "brand_id"} else v for i, v in enumerate(r)]
                    self.conn._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", vals)

        def drop(self, module_id: str) -> None:
            if self.conn is not None:
                self.conn.close()
                self.conn = None

    db = _ExportableSqliteModuleDatabase()
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.4.16"}, PlacementStore(tmp_path / "p.json"))
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda p: None, database=db, registry=registry,
                                system_versions={"core_runtime": "0.0.4.16"})
    assert lifecycle.install_bundled("storyboard")["state"] == "INSTALLED"
    registry.approve_permissions("storyboard", ["storyboard.read"])
    assert lifecycle.activate("storyboard")["state"] == "ACTIVE"

    lib = ScenarioLibraryStore(lambda: db.conn)
    created = lib.create_scenario({
        "owner_brand_id": "brand-a",
        "title": "Бекап бібліотеки",
        "scenes": [{"prompt": "Сцена для відновлення", "characters": ["did_samogon"], "duration": 5}],
    }, "admin")
    sid = created["scenario_id"]
    lib.save_version(sid, "v1", "admin", set(), "admin")
    lib.grant_share(sid, "brand-b", "admin", set(), "admin")
    copy = lib.copy_scenario(sid, "brand-b", "admin", set(), "admin", version=1)

    # Snapshot + uninstall archive -> drop DB -> restore from snapshot -> verify full fidelity.
    snapshot_dir = lifecycle._snapshot("storyboard", "backup-op")
    db.drop("storyboard")
    assert not db.exists("storyboard")
    lifecycle._restore_snapshot("storyboard", snapshot_dir)
    assert db.exists("storyboard")

    restored_lib = ScenarioLibraryStore(lambda: db.conn)
    detail = restored_lib.get_scenario(sid, "admin", set())
    assert detail["title"] == "Бекап бібліотеки"
    assert detail["latest_version"] == 1
    assert detail["shared_brands"] == ["brand-b"]
    assert len(detail["versions"]) == 1
    assert restored_lib.get_scenario(copy["scenario_id"], "admin", set())["copied_from_scenario_id"] == sid
    assert [e["action"] for e in restored_lib.audit_records(sid, "admin", set())] == ["create", "save_version", "share_grant"]

