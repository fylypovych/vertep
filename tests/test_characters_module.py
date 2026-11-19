"""i.0.0.4.99 (#499): Characters module — ownership, collaboration, activation."""

import json
import uuid
from http.client import HTTPConnection
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.module.lifecycle import ModuleLifecycle
from core.state import store
from core.workflow.runtime import WorkflowRuntime
from tests.module_postgres import module_postgres  # noqa: F401


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.app import app
    from core.state import store
    from core.workflow.runtime import WorkflowRuntime
    dag = tmp_path / "dag"
    dag.mkdir()
    (dag / "c.json").write_text(json.dumps({"name": "C", "version": "1.0.0", "tasks": [{"id": "a", "type": "text_generation"}]}))
    for name, value in {"VERTEP_MODULE_LIFECYCLE_ROOT": str(tmp_path / "lc"), "VERTEP_MODULE_PLACEMENT_FILE": str(tmp_path / "p.json"),
                        "WORKFLOW_STATE_ROOT": str(tmp_path / "s"), "VERTEP_MODULE_AUDIT_FILE": str(tmp_path / "a.jsonl"),
                        "VERTEP_CORE_URL": "http://localhost:8080", "VERTEP_MODULE_TOKEN": "test-token",
                        "CHARACTERS_ROOT": str(tmp_path / "characters")}.items():
        monkeypatch.setenv(name, value)
    runtime = WorkflowRuntime(dag)
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: runtime)
    # Add worker nodes for module catalog
    nodes = {"text-node": {"role": "text", "gpu_available": False, "vram_mb": 0, "cpu_cores": 4, "ram_mb": 8192},
             "gpu-node": {"role": "gpu", "gpu_available": True, "vram_mb": 12288, "cpu_cores": 8, "ram_mb": 32768}}
    for name, facts in nodes.items():
        monkeypatch.setitem(store.workers, name, {"node_name": name, "status": "READY", **facts})
    client = TestClient(app)
    client.runtime = runtime
    return client


@pytest.fixture
def characters_db(api, tmp_path, monkeypatch, module_postgres):
    import pg8000.dbapi
    from modules.characters import characters_logic

    parsed = urlparse(module_postgres)
    connection = pg8000.dbapi.connect(
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path.lstrip("/"),
    )
    connection.autocommit = True
    schema = f"characters_test_{uuid.uuid4().hex[:12]}"
    cursor = connection.cursor()
    cursor.execute(f'CREATE SCHEMA "{schema}"')
    cursor.execute(f'SET search_path TO "{schema}"')
    monkeypatch.setattr(characters_logic, "database", lambda: connection)
    characters_root = tmp_path / "characters-db"
    monkeypatch.setenv("CHARACTERS_ROOT", str(characters_root))
    module = characters_logic.CharactersModule()
    try:
        yield module
    finally:
        cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
        connection.close()


def complete_profile(character_id, name="Test Character"):
    return {
        "id": character_id,
        "name": name,
        "language": "uk",
        "enabled": True,
        "system_prompt": "A complete profile",
        "voice": {"provider": "none"},
        "visual": {"style": "documentary"},
        "generation": {"steps": 20},
        "publishing": {"enabled": False},
    }


def test_characters_module_in_bundled(api):
    """Characters module should be available as bundled module."""
    catalog = api.get("/api/nodes/module-catalog").json()
    module_ids = {m["module_id"] for m in catalog["modules"]}
    assert "characters" in module_ids
    char_module = next(m for m in catalog["modules"] if m["module_id"] == "characters")
    assert char_module["kind"] == "application"
    assert char_module["label"] == "Персонажі"


def test_characters_module_install_and_lifecycle(api):
    """Test install, activate, deactivate of characters module."""
    # Install (bundled module)
    resp = api.post("/api/nodes/module-registry/lifecycle/install", json={"bundled": "characters"})
    # May be already installed in some test runs
    assert resp.status_code in {200, 409}
    if resp.status_code == 200:
        assert resp.json()["state"] == "INSTALLED"

    # Activate (requires placement and approval)
    resp = api.put("/api/nodes/module-registry/placement/characters", json={"node_ids": ["text-node"]})
    assert resp.status_code == 200

    resp = api.put("/api/nodes/module-registry/approval/characters", json={"permissions": ["characters.read", "characters.write", "characters.collaborate"]})
    assert resp.status_code == 200

    # Activation with the module's own schema, API/UI, archive and restore runs on a
    # disposable PostgreSQL in tests/test_characters_lifecycle_e2e.py.


def test_characters_module_catalog_entry(api):
    """Test characters module appears in catalog with correct metadata."""
    catalog = api.get("/api/nodes/module-catalog").json()
    char_module = next(m for m in catalog["modules"] if m["module_id"] == "characters")
    assert char_module["kind"] == "application"
    assert char_module["scaling"] == "single_owner"
    assert char_module["requires_gpu"] is False
    assert "characters.read" in char_module["permissions"]
    assert "characters.write" in char_module["permissions"]
    assert "characters.collaborate" in char_module["permissions"]


def test_characters_module_requires_text_node(api):
    """Characters module should be compatible with text-node (no GPU required)."""
    catalog = api.get("/api/nodes/module-catalog").json()
    char_module = next(m for m in catalog["modules"] if m["module_id"] == "characters")
    text_node = next(n for n in char_module["nodes"] if n["node_id"] == "text-node")
    assert text_node["compatible_reasons"] == []


def test_characters_module_incompatible_with_gpu_only(api):
    """Characters module should work on gpu-node too (no GPU required but compatible)."""
    catalog = api.get("/api/nodes/module-catalog").json()
    char_module = next(m for m in catalog["modules"] if m["module_id"] == "characters")
    gpu_node = next(n for n in char_module["nodes"] if n["node_id"] == "gpu-node")
    assert gpu_node["compatible_reasons"] == []


def test_characters_ac2_ownership(api, characters_db):
    """AC2: Each character has exactly one brand owner; non-owner cannot modify profile."""
    character = characters_db.create_character("brand-a", complete_profile("character-a"))
    assert character["owner_brand_id"] == "brand-a"
    assert character["active"] is False
    with pytest.raises(ValueError, match="Not owner"):
        characters_db.update_character("character-a", "brand-b", {"name": "Hijacked"})
    with pytest.raises(ValueError, match="Not owner"):
        characters_db.delete_character("character-a", "brand-b")
    assert characters_db.get_character("character-a", "brand-b") is None


def test_characters_ac3_active_availability(api, characters_db):
    """AC3: New characters are not open for collaboration by default; active characters available to all."""
    characters_db.create_character("brand-a", complete_profile("character-a"))
    assert characters_db.get_character("character-a", "brand-b") is None
    activated = characters_db.activate_character("character-a", "brand-a")
    assert activated["active"] is True
    assert characters_db.get_character("character-a", "brand-b")["is_owner"] is False
    assert [item["id"] for item in characters_db.list_characters("brand-b")] == ["character-a"]
    from modules.characters import characters_logic
    assert not characters_logic.query(
        "SELECT 1 FROM character_collaborations WHERE character_id = %s", ("character-a",)
    )


def test_characters_ac4_deactivation_preserves_references(api, characters_db):
    """AC4: Deactivation blocks new collaborations but keeps references/materials."""
    characters_db.create_character("brand-a", complete_profile("character-a"))
    characters_db.activate_character("character-a", "brand-a")
    uploaded = characters_db.upload_reference("character-a", "brand-a", b"\x89PNG\r\n\x1a\nreference")

    deactivated = characters_db.deactivate_character("character-a", "brand-a")
    assert deactivated["active"] is False
    assert characters_db.get_character("character-a", "brand-b") is None
    from modules.characters import characters_logic
    references = characters_logic.query(
        "SELECT filename FROM character_references WHERE character_id = %s", ("character-a",)
    )
    assert references[0][0] == uploaded["filename"]
    assert (characters_db.characters_root / "character-a" / "references" / uploaded["filename"]).is_file()
    assert characters_db.check_generation_allowed("character-a") == (False, ["Character is not active"])
    assert characters_db.check_generation_allowed("character-a", "brand-b") == (False, ["Character is not active"])
    assert characters_db.check_generation_allowed("character-a", "brand-a") == (True, [])  # the owner keeps using it


def test_characters_ac5_incomplete_profile_blocks_generation(api, characters_db):
    """AC5: Incomplete profile blocks new generation with explanation of missing fields."""
    characters_db.create_character("brand-a", {"id": "incomplete", "name": "Incomplete"})
    allowed, reasons = characters_db.check_generation_allowed("incomplete")
    assert allowed is False
    assert "system_prompt.txt" in reasons[0]
    assert "voice.json" in reasons[0]


def test_characters_ac5_invalid_profile_json_is_not_ready(api, characters_db):
    characters_db.create_character("brand-a", complete_profile("character-a"))
    voice_path = characters_db.characters_root / "character-a" / "voice.json"
    voice_path.write_text("[]", encoding="utf-8")
    allowed, reasons = characters_db.check_generation_allowed("character-a")
    assert allowed is False
    assert reasons == ["Incomplete profile: missing voice.json must contain an object"]
    with pytest.raises(ValueError, match="voice.json must contain an object"):
        characters_db.activate_character("character-a", "brand-a")


def test_characters_ac6_migration_requires_explicit_owner_and_preserves_data(tmp_path, monkeypatch):
    """AC6: Migration requires an owner and preserves the character ID, profile, and references."""
    monkeypatch.setenv("VERTEP_CORE_URL", "http://localhost")
    monkeypatch.setenv("VERTEP_MODULE_TOKEN", "test-token")
    from modules.characters import characters_logic

    legacy_root = tmp_path / "legacy"
    character_dir = legacy_root / "character-a"
    references_dir = character_dir / "references"
    references_dir.mkdir(parents=True)
    (character_dir / "character.json").write_text(json.dumps({
        "id": "character-a", "name": "Test Character", "language": "uk", "enabled": True,
    }), encoding="utf-8")
    (character_dir / "system_prompt.txt").write_text("A test prompt", encoding="utf-8")
    for name in ("voice", "visual", "generation", "publishing"):
        (character_dir / f"{name}.json").write_text(json.dumps({"source": name}), encoding="utf-8")
    (references_dir / "reference.png").write_bytes(b"reference-bytes")

    statements = []
    monkeypatch.setattr(characters_logic, "query", lambda *args: [])
    monkeypatch.setattr(characters_logic, "execute", lambda sql, args=(): statements.append((sql, args)))
    module_root = tmp_path / "module-characters"
    module = type("TestModule", (), {"characters_root": module_root})()

    missing_owner = characters_logic.migrate_existing_characters(module, legacy_root)
    assert missing_owner["migrated"] == []
    assert missing_owner["skipped"][0]["id"] == "*"
    assert statements == []

    result = characters_logic.migrate_existing_characters(module, legacy_root, " owner-brand ")
    assert result == {"migrated": ["character-a"], "skipped": [], "errors": []}
    character_insert = next(args for sql, args in statements if "INSERT INTO characters (" in sql)
    profile_insert = next(args for sql, args in statements if "INSERT INTO character_profiles" in sql)
    assert character_insert == ("character-a", "Test Character", "uk", True, None, "owner-brand")
    assert profile_insert[0:2] == ("character-a", "A test prompt")
    assert json.loads(profile_insert[2]) == {"source": "voice"}
    assert json.loads(profile_insert[3]) == {"source": "visual"}
    assert json.loads(profile_insert[4]) == {"source": "generation"}
    assert json.loads(profile_insert[5]) == {"source": "publishing"}
    assert all((module_root / "character-a" / name).is_file() for name in characters_logic.REQUIRED_PROFILE_FILES)
    assert characters_logic._character_profile_complete("character-a", module_root) == (True, [])
    assert (module_root / "character-a" / "references" / "reference.png").read_bytes() == b"reference-bytes"


def test_characters_ac7_module_contract(api):
    """AC7: Characters module follows module contract with proper lifecycle."""
    # Module should install, activate, deactivate via standard lifecycle
    # Should have proper manifest with permissions
    catalog = api.get("/api/nodes/module-catalog").json()
    char_module = next(m for m in catalog["modules"] if m["module_id"] == "characters")

    # Verify module has required permissions
    assert "characters.read" in char_module["permissions"]
    assert "characters.write" in char_module["permissions"]
    assert "characters.collaborate" in char_module["permissions"]

    # Verify module kind and placement
    assert char_module["kind"] == "application"
    assert char_module["scaling"] == "single_owner"
    assert char_module["requires_gpu"] is False


def test_characters_get_nested_routes_and_migration_endpoint(monkeypatch):
    monkeypatch.setenv("VERTEP_CORE_URL", "http://core.invalid")
    monkeypatch.setenv("VERTEP_MODULE_TOKEN", "module-token")
    from modules.characters import characters_module
    from modules.characters import characters_logic

    class FakeCharactersModule:
        characters_root = Path("/module-characters")

        def get_character(self, character_id, brand_id):
            return {"id": character_id, "references": [{"filename": "ref.png"}]}

        def check_generation_allowed(self, character_id, brand_id=None):
            return brand_id == "brand-a", [] if brand_id == "brand-a" else ["Character is not active"]

    monkeypatch.setattr(characters_module, "TOKEN", "module-token")
    monkeypatch.setattr(characters_module, "health_check", lambda: {"ready": True})
    monkeypatch.setattr(characters_module, "CharactersModule", FakeCharactersModule)
    monkeypatch.setattr(characters_logic, "migrate_existing_characters", lambda module, root, owner: {"owner": owner})
    server = ThreadingHTTPServer(("127.0.0.1", 0), characters_module.Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1])
        connection.request("GET", "/health")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"ready": True}
        headers = {"x-vertep-module-token": "module-token", "x-vertep-user": "brand-a"}

        def request_json(method, path, role, body=None):
            request_headers = {**headers, "x-vertep-role": role}
            if body is not None:
                request_headers["content-type"] = "application/json"
            connection.request(method, path, json.dumps(body).encode() if body is not None else None,
                               headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())

        connection.request("GET", "/characters/character-a/references", headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200, body
        assert body == [{"filename": "ref.png"}]
        connection.request("GET", "/characters/character-a/can-generate", headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200, body
        assert body == {"allowed": False, "reasons": ["Character is not active"], "owner_brand_id": None}  # the user is not a brand
        connection.request("GET", "/characters/character-a/can-generate?brand_id=brand-a", headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200, body
        assert body == {"allowed": True, "reasons": [], "owner_brand_id": None}
        assert request_json("POST", "/characters/character-a/activate?brand_id=brand-a", "moderator")[0] == 403
        assert request_json("POST", "/migrate", "editor", {"default_owner_brand": "brand-a"})[0] == 403
        assert request_json("POST", "/migrate", "admin")[0] == 400
        assert request_json("POST", "/migrate", "admin", {"default_owner_brand": " brand-a "}) == (
            200, {"owner": "brand-a"}
        )
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()



def test_core_job_creation_follows_owner_and_activation_and_spares_existing_jobs(api, characters_db, monkeypatch):
    """#499 AC2-AC4/AC7: with the Characters module READY, CORE asks it before a new
    job: the owner brand may always use its character, another brand only while it
    is active; deactivation blocks new jobs only, an already created job stays."""
    from core.module.gateway import issue_token
    from core.module.manifest import ModuleManifest
    from modules.characters import characters_module
    from core.module.lifecycle import bundled_manifest

    monkeypatch.setattr(characters_module, "TOKEN", issue_token("characters", "text-node"))
    monkeypatch.setattr(characters_module, "CharactersModule", lambda: characters_db)
    server = ThreadingHTTPServer(("127.0.0.1", 0), characters_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        registry = api.runtime.modules
        manifest = ModuleManifest.from_dict(bundled_manifest("characters"))
        registry.report_node("text-node", [{**manifest.to_dict(), "ready": True,
                                            "endpoint": f"http://127.0.0.1:{server.server_address[1]}"}])
        registry.set_placement("characters", ["text-node"])
        registry.approve_permissions("characters", list(manifest.permissions))
        assert registry.ready_instances("characters")
        monkeypatch.setenv("CHARACTERS_ROOT", str(characters_db.characters_root))
        characters_db.create_character("brand-a", complete_profile("character-a"))

        def create(brand):
            return api.post("/api/jobs", json={"topic": "Тест", "character_id": "character-a", "brand_id": brand})

        refused = create("brand-b")
        assert refused.status_code == 422 and "not active" in refused.json()["detail"], refused.text
        owner = create("brand-a")  # the owner does not need activation
        assert owner.status_code == 200, owner.text
        characters_db.activate_character("character-a", "brand-a")
        collaborating = create("brand-b")
        assert collaborating.status_code == 200, collaborating.text
        before = {job_id for job_id, job in store.jobs.items() if job.character_id == "character-a"}
        characters_db.deactivate_character("character-a", "brand-a")
        assert create("brand-b").status_code == 422
        after = {job_id for job_id, job in store.jobs.items() if job.character_id == "character-a"}
        assert before <= after  # existing jobs are neither removed nor re-checked
        # A READY module that does not answer fails closed instead of skipping the check.
        server.shutdown()
        server.server_close()
        assert create("brand-a").status_code == 503
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        thread.join(timeout=5)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])