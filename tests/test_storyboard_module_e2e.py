"""#501 (owner decision 19.11.2026): the Storyboard module has no database of its
own; it reads the storyboards that live in the jobs through CORE's module API
and shows them per brand. Revisions and approvals stay in the job flow."""

import json
import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from core.models import StoryboardScene, StoryboardVersion
from core.module.instances import ModuleInstanceRegistry, PlacementStore
from core.module.lifecycle import ModuleLifecycle, bundled_manifest
from core.module.manifest import ModuleManifest
from tests.test_characters_module import api  # noqa: F401  (fixture)

MODULE_DIR = Path(__file__).resolve().parents[1] / "modules" / "storyboard"


def _version(version, status, prompts, image=None):
    return StoryboardVersion(version=version, title=f"v{version}", status=status, scenes=[
        StoryboardScene(index=i + 1, prompt=prompt, video_prompt=prompt, duration=2.5,
                        image_artifact_id=image if i == 0 else None)
        for i, prompt in enumerate(prompts)])


@pytest.fixture
def storyboard(api, tmp_path, monkeypatch):
    from core.models import ArtifactRecord
    from core.module.gateway import issue_token
    from core.state import store
    monkeypatch.syspath_prepend(str(MODULE_DIR))
    import storyboard_module
    # Platform lifecycle: install/activate provisions the module schema (#508).
    class _StubModuleDatabase:
        def __init__(self):
            self.migrated = []
        def exists(self, module_id): return False
        def ensure(self, module_id):
            return {"role": f"mod_{module_id}", "schema": f"mod_{module_id}", "password": "pw", "database": "vertep"}
        def migrate(self, module_id, migrations, sources):
            self.migrated.extend(m["id"] for m in migrations)
            return [m["id"] for m in migrations]
        def drop(self, module_id): pass

    db = _StubModuleDatabase()
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.4.12"}, PlacementStore(tmp_path / "p.json"))
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda package: None, database=db, registry=registry,
                                system_versions={"core_runtime": "0.0.4.12"})
    assert lifecycle.install_bundled("storyboard")["state"] == "INSTALLED"
    registry.approve_permissions("storyboard", ["storyboard.read"])
    assert lifecycle.activate("storyboard")["state"] == "ACTIVE"
    assert db.migrated == ["m001_initial"]
    # The module talks to CORE with its token (TestClient stands in for the HTTP hop).
    monkeypatch.setattr(storyboard_module, "TOKEN", issue_token("storyboard", "text-node"))

    def core(path):
        response = api.get(path, headers={"x-vertep-module-token": storyboard_module.TOKEN, "Authorization": ""})
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise storyboard_module.CoreUnavailable(str(response.status_code))
        return response.json()
    monkeypatch.setattr(storyboard_module, "_core", core)
    server = ThreadingHTTPServer(("127.0.0.1", 0), storyboard_module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
    core_registry = api.runtime.modules
    core_registry.report_node("text-node", [{**manifest.to_dict(), "ready": True,
                                             "endpoint": f"http://127.0.0.1:{server.server_address[1]}"}])
    core_registry.set_placement("storyboard", ["text-node"])
    core_registry.approve_permissions("storyboard", ["storyboard.read"])
    # Two jobs with storyboards; job A uses a character owned by brand B.
    job_a = store.create("Ранкова кава", "hero", 5, "web", "image", 0, "brand-a")
    job_c = store.create("Вечір", "other", 5, "web", "image", 0, "brand-c")
    image = ArtifactRecord(artifact_id="img-1", kind="image", path="storyboard/scene-1.png", filename="scene-1.png",
                           mime_type="image/png", size=3, sha256="ab" * 32, created_at="2026-11-19T00:00:00+00:00")
    with store.lock:
        job_a.character_owner_brand = "brand-b"
        job_a.artifacts.append(image)
        job_a.storyboards = [_version(1, "pending_approval", ["Кухня", "Чашка"], image="img-1")]
        job_a.active_storyboard_version = 1
        job_c.storyboards = [_version(1, "approved", ["Місто"])]
        job_c.active_storyboard_version = 1
    yield {"server": server, "job_a": job_a, "job_c": job_c, "module": storyboard_module, "registry": core_registry}
    server.shutdown()


def _get(storyboard, path, role, brands, token=True):
    connection = HTTPConnection("127.0.0.1", storyboard["server"].server_address[1], timeout=10)
    headers = {"x-vertep-role": role, "x-vertep-brands": brands}
    if token:
        headers["x-vertep-module-token"] = storyboard["module"].TOKEN
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    body = json.loads(response.read() or b"null")
    connection.close()
    return response.status, body


def test_storyboards_come_from_the_jobs_and_are_scoped_by_brand(storyboard):
    a, c = storyboard["job_a"].job_id, storyboard["job_c"].job_id
    status, items = _get(storyboard, "/storyboards", "admin", "*")
    assert status == 200 and {item["job_id"] for item in items} >= {a, c}
    status, items = _get(storyboard, "/storyboards", "moderator", "brand-b")  # owner of A's character
    assert [item["job_id"] for item in items] == [a] and items[0]["character_owner_brands"] == ["brand-b"]
    assert [item["job_id"] for item in _get(storyboard, "/storyboards", "moderator", "brand-a")[1]] == [a]
    assert _get(storyboard, "/storyboards", "moderator", "brand-x")[1] == []
    status, detail = _get(storyboard, f"/storyboards/{a}", "moderator", "brand-a")
    scenes = detail["storyboards"][0]["scenes"]
    assert [scene["prompt"] for scene in scenes] == ["Кухня", "Чашка"]  # scene order kept
    assert scenes[0]["image"] == {"artifact_id": "img-1", "sha256": "ab" * 32} and scenes[0]["duration"] == 2.5
    assert _get(storyboard, f"/storyboards/{c}", "moderator", "brand-a")[0] == 404  # another brand's job
    assert _get(storyboard, "/storyboards", "admin", "*", token=False)[0] == 403


def test_a_revision_in_the_job_shows_up_and_core_refuses_other_callers(storyboard, api):
    from core.state import store
    job = storyboard["job_a"]
    with store.lock:  # the job's own revision flow produced a new version
        job.storyboards[0].status = "superseded"
        job.storyboards.append(_version(2, "pending_approval", ["Кухня", "Чашка", "Сад"]))
        job.active_storyboard_version = 2
    status, detail = _get(storyboard, f"/storyboards/{job.job_id}", "moderator", "brand-b")
    assert status == 200 and detail["active_version"] == 2
    assert [v["status"] for v in detail["storyboards"]] == ["superseded", "pending_approval"]
    # Only the Storyboard module with its approved permission reads them from CORE.
    from core.module.gateway import issue_token
    assert api.get("/api/modules/self/storyboards", headers={"Authorization": ""}).status_code in {401, 403}
    other = api.get("/api/modules/self/storyboards",
                    headers={"x-vertep-module-token": issue_token("characters", "text-node"), "Authorization": ""})
    assert other.status_code in {401, 403}
    # Without the module, the job's storyboard API in CORE keeps working.
    storyboard["registry"].remove_node("text-node")
    assert api.get(f"/api/jobs/{job.job_id}").status_code == 200
