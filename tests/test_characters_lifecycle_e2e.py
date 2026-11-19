"""#499 AC1/AC4/AC6/AC7: the Characters module end to end on a disposable PostgreSQL.

The bundled package is installed and activated through the platform lifecycle
(own schema/role, signed migrations, data folder); the module's own HTTP API and
UI run against the module role; legacy characters are migrated with an explicit
owner; CORE asks the module before new jobs; deactivation spares running and
pending-approval material; the admin-only archive restores IDs, ownership,
references and history.
"""

import base64
import hashlib
import hmac
import json
import threading
import time
from http.client import HTTPConnection
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
from tests.test_characters_module import api, complete_profile  # noqa: F401  (fixture)

PNG = b"\x89PNG\r\n\x1a\n" + b"legacy-reference"


def _module_connection(dsn, credentials):
    parsed = urlparse(dsn)
    connection = pg8000.dbapi.connect(user=credentials["role"], password=credentials["password"],
                                      host=parsed.hostname, port=parsed.port, database=unquote(parsed.path.lstrip("/")))
    connection.autocommit = True
    return connection


@pytest.fixture
def installed(api, tmp_path, monkeypatch, module_postgres):
    from modules.characters import characters_logic, characters_module
    from core.module.gateway import issue_token
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.4.11"}, PlacementStore(tmp_path / "placement.json"))
    credentials = {}
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda package: None, database=ModuleDatabase(module_postgres),
                                registry=registry, system_versions={"core_runtime": "0.0.4.11"},
                                store_credentials=lambda module_id, value: credentials.__setitem__(module_id, value),
                                drain_timeout=1.0)
    assert lifecycle.install_bundled("characters")["state"] == "INSTALLED"
    registry.approve_permissions("characters", ["characters.read", "characters.write", "characters.collaborate"])
    assert lifecycle.activate("characters")["state"] == "ACTIVE"
    connection = _module_connection(module_postgres, credentials["characters"])
    monkeypatch.setattr(characters_logic, "database", lambda: connection)
    files = lifecycle.data_dir("characters") / "files" / "characters"
    monkeypatch.setenv("CHARACTERS_ROOT", str(files))
    module = characters_logic.CharactersModule()
    monkeypatch.setattr(characters_module, "TOKEN", issue_token("characters", "text-node"))
    monkeypatch.setattr(characters_module, "CharactersModule", lambda: module)
    server = ThreadingHTTPServer(("127.0.0.1", 0), characters_module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # CORE sees the module READY on its node (placement + approval + report).
    core_registry = api.runtime.modules
    manifest = ModuleManifest.from_dict(bundled_manifest("characters"))
    core_registry.report_node("text-node", [{**manifest.to_dict(), "ready": True,
                                             "endpoint": f"http://127.0.0.1:{server.server_address[1]}"}])
    core_registry.set_placement("characters", ["text-node"])
    core_registry.approve_permissions("characters", list(manifest.permissions))
    assert core_registry.ready_instances("characters")
    yield {"lifecycle": lifecycle, "module": module, "server": server, "dsn": module_postgres,
           "credentials": credentials, "files": files, "connection": connection, "api": api}
    server.shutdown()
    connection.close()


def _call(installed, method, path, *, role="moderator", brands="brand-b", body=None):
    from modules.characters import characters_module
    connection = HTTPConnection("127.0.0.1", installed["server"].server_address[1], timeout=10)
    headers = {"x-vertep-module-token": characters_module.TOKEN, "x-vertep-role": role, "x-vertep-brands": brands,
               "x-vertep-user": "moderator-b", "content-type": "application/json"}
    connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    try:
        return response.status, json.loads(data)
    except ValueError:
        return response.status, data


def _rows(installed, sql):
    cursor = installed["connection"].cursor()
    cursor.execute(sql)
    return [list(row) for row in cursor.fetchall()]


def test_installed_package_has_its_schema_files_api_and_ui(installed):
    tables = {row[0] for row in _rows(installed, "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
    assert {"characters", "character_profiles", "character_references", "character_audit", "_vertep_migrations"} <= tables
    assert _rows(installed, "SELECT id FROM _vertep_migrations") == [["m001_initial"]]
    assert installed["lifecycle"].data_dir("characters").is_dir()
    status, ui = _call(installed, "GET", "/ui/index.html")
    assert status == 200 and b"/api/modules/characters/v1" in ui
    status, created = _call(installed, "POST", "/characters?brand_id=brand-b", body=complete_profile("hero", "Герой"))
    assert status == 201 and created["owner_brand_id"] == "brand-b" and created["active"] is False
    assert (installed["files"] / "hero" / "character.json").is_file()
    status, _ = _call(installed, "POST", "/characters?brand_id=brand-c", body=complete_profile("x"))
    assert status == 403  # a moderator of brand-b cannot act for brand-c


def test_deactivation_spares_running_and_pending_material_and_archive_restores_everything(installed, tmp_path, monkeypatch):
    from core.models import JobStatus, VideoVersion
    from core.state import store
    from modules.characters.characters_logic import migrate_existing_characters
    api = installed["api"]

    # -- legacy characters migrate with an explicit owner, IDs and references kept --
    legacy = tmp_path / "legacy"
    hero = legacy / "legacy-hero"
    hero.mkdir(parents=True)
    profile = complete_profile("legacy-hero", "Легенда")
    (hero / "character.json").write_text(json.dumps({k: profile[k] for k in ("id", "name", "language", "enabled")}))
    (hero / "system_prompt.txt").write_text(profile["system_prompt"])
    for name in ("voice", "visual", "generation", "publishing"):
        (hero / f"{name}.json").write_text(json.dumps(profile[name]))
    (hero / "references").mkdir()
    (hero / "references" / "face.png").write_bytes(PNG)
    assert migrate_existing_characters(installed["module"], legacy, None)["migrated"] == []  # no silent owner
    result = migrate_existing_characters(installed["module"], legacy, "brand-b")
    assert result["migrated"] == ["legacy-hero"] and not result["errors"]
    assert _call(installed, "POST", "/characters/legacy-hero/activate?brand_id=brand-b")[0] == 200

    # -- brand A uses B's active character: one job runs, one waits for approval --
    def create(brand):
        return api.post("/api/jobs", json={"topic": "Спільний", "character_id": "legacy-hero", "brand_id": brand})
    running, pending = create("brand-a"), create("brand-a")
    assert running.status_code == 200 and pending.status_code == 200, running.text
    running_job, pending_job = store.jobs[running.json()["job_id"]], store.jobs[pending.json()["job_id"]]
    assert running_job.character_owner_brand == pending_job.character_owner_brand == "brand-b"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not all(
            any("SCRIPT TASK QUEUED" in event for event in job.events) for job in (running_job, pending_job)):
        time.sleep(0.02)
    video = store.root / pending_job.job_id / "final" / "video-v1.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"material v1")
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    with store.lock:
        pending_job.video_versions = [VideoVersion(version=1, path="final/video-v1.mp4", sha256=digest, approved=True)]
        pending_job.active_video_version = 1
        pending_job.output_path = str(video)
        store.update(pending_job, JobStatus.READY, "READY (material awaiting the owner)")
    running_status = store.jobs[running_job.job_id].status

    # -- the owner deactivates: new collaborations stop, existing material does not --
    assert _call(installed, "POST", "/characters/legacy-hero/deactivate?brand_id=brand-b")[0] == 200
    refused = create("brand-a")
    assert refused.status_code == 422 and "not active" in refused.json()["detail"]
    assert store.jobs[running_job.job_id].status == running_status  # not cancelled or re-checked
    # The generation that was already running finishes its step after the deactivation.
    from tests.test_features import _complete_script_task
    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    _complete_script_task(api, running_job.job_id)
    finished = store.jobs[running_job.job_id]
    assert finished.script is not None and finished.status != running_status, finished.status
    decision = api.post(f"/api/jobs/{pending_job.job_id}/shared-approval",
                        json={"owner_brand": "brand-b", "decision": "approve", "version": 1, "expected_sha256": digest})
    assert decision.status_code == 200 and decision.json()["owners"] == {"brand-b": "approved"}
    from core.shared_content import publication_allowed
    assert publication_allowed(store.jobs[pending_job.job_id], store.root) == (True, None)
    assert create("brand-b").status_code == 200  # the owner itself keeps using it

    # -- archive before removal, then restore: IDs, ownership, references, history --
    lifecycle = installed["lifecycle"]
    archive_name = lifecycle.uninstall("characters", confirmed=True)["archive"]
    assert not (installed["files"] / "legacy-hero").exists()
    restored = lifecycle.restore_archive(lifecycle.root / "archives" / archive_name)
    assert restored["state"] == "INSTALLED"
    connection = _module_connection(installed["dsn"], installed["credentials"]["characters"])
    cursor = connection.cursor()
    cursor.execute("SELECT id, owner_brand_id, active FROM characters ORDER BY id")
    assert [list(row) for row in cursor.fetchall()] == [["legacy-hero", "brand-b", False]]
    cursor.execute("SELECT filename, sha256 FROM character_references WHERE character_id = 'legacy-hero'")
    assert [list(row) for row in cursor.fetchall()] == [["face.png", hashlib.sha256(PNG).hexdigest()]]
    cursor.execute("SELECT action FROM character_audit WHERE character_id = 'legacy-hero' ORDER BY id")
    assert [row[0] for row in cursor.fetchall()] == ["migrate", "activate", "deactivate"]
    connection.close()
    assert (installed["files"] / "legacy-hero" / "references" / "face.png").read_bytes() == PNG
    assert store.jobs[pending_job.job_id].character_id == "legacy-hero"  # existing jobs keep the global ID


def test_changing_the_character_is_checked_and_adds_its_owner(installed):
    """#500 AC4 with #499: switching a job to another brand's character goes through
    the module check, and both owners' decisions are then required."""
    from core.shared_content import foreign_owners
    from core.state import store
    api = installed["api"]
    for character, brand in (("hero", "brand-b"), ("villain", "brand-c")):
        status, _ = _call(installed, "POST", f"/characters?brand_id={brand}", role="admin", brands="*",
                          body=complete_profile(character))
        assert status == 201
    created = api.post("/api/jobs", json={"topic": "Двоє", "character_id": "hero", "brand_id": "brand-a"})
    assert created.status_code == 422  # hero is not active for collaboration yet
    assert _call(installed, "POST", "/characters/hero/activate?brand_id=brand-b")[0] == 200
    created = api.post("/api/jobs", json={"topic": "Двоє", "character_id": "hero", "brand_id": "brand-a"})
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    refused = api.patch(f"/api/jobs/{job_id}", json={"character_id": "villain"})
    assert refused.status_code == 422 and "not active" in refused.json()["detail"]
    assert _call(installed, "POST", "/characters/villain/activate?brand_id=brand-c", role="admin", brands="*")[0] == 200
    assert api.patch(f"/api/jobs/{job_id}", json={"character_id": "villain"}).status_code == 200
    assert foreign_owners(store.jobs[job_id]) == ["brand-b", "brand-c"]
