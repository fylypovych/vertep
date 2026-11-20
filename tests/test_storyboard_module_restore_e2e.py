"""#501 AC5: Storyboard history survives a real Backup Service restore.

The storyboard is produced by the existing CORE flow (Text Worker, image Worker,
a revision through ``/storyboards/regenerate`` and the approval), read through
the Storyboard module, backed up by the Backup Service process, damaged on
disk, restored and read again after a CORE store restart. Job/version/scene
IDs, revision history, approval, references and image hashes must match.
"""

import hashlib
import json
import shutil
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests import media_harness as harness
from tests.test_backup_process_acceptance import backup_process, challenge  # noqa: F401  (fixture)
from tests.test_full_media_harness import client, fleet  # noqa: F401  (fixtures)

MODULE_DIR = Path(__file__).resolve().parents[1] / "modules" / "storyboard"


def _module(fleet, monkeypatch, tmp_path):
    from core.module.gateway import issue_token
    from core.module.instances import ModuleInstanceRegistry, PlacementStore
    from core.module.lifecycle import bundled_manifest
    from core.module.manifest import ModuleManifest
    monkeypatch.syspath_prepend(str(MODULE_DIR))
    import storyboard_module
    monkeypatch.setenv("VERTEP_MODULE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.4.13"}, PlacementStore(tmp_path / "placement.json"))
    monkeypatch.setattr("core.module.gateway._modules_or_none", lambda: registry)
    monkeypatch.setattr(storyboard_module, "TOKEN", issue_token("storyboard", "app-node"))

    def core(path):  # the module's HTTP hop to CORE (TestClient stands in for the socket)
        response = fleet.get(path, headers={"x-vertep-module-token": storyboard_module.TOKEN})
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise storyboard_module.CoreUnavailable(str(response.status_code))
        return response.json()
    monkeypatch.setattr(storyboard_module, "_core", core)
    server = ThreadingHTTPServer(("127.0.0.1", 0), storyboard_module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
    registry.report_node("app-node", [{**manifest.to_dict(), "ready": True,
                                       "endpoint": f"http://127.0.0.1:{server.server_address[1]}"}])
    registry.set_placement("storyboard", ["app-node"])
    registry.approve_permissions("storyboard", list(manifest.permissions))

    def read(job_id):
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request("GET", f"/storyboards/{job_id}", headers={
            "x-vertep-module-token": storyboard_module.TOKEN, "x-vertep-role": "admin"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        assert response.status == 200, body
        return body
    return server, read


def _image_hashes(job_id, detail):
    from core.state import store
    job = store.jobs[job_id]
    paths = {a.artifact_id: Path(store.root) / job_id / a.path for a in job.artifacts}
    result = {}
    for version in detail["storyboards"]:
        for scene in version["scenes"]:
            if scene["image"]:
                path = paths[scene["image"]["artifact_id"]]
                result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
                assert result[str(path)] == scene["image"]["sha256"]
    return result


def test_storyboard_history_is_read_back_by_the_module_after_a_backup_service_restore(
        fleet, backup_process, monkeypatch, tmp_path):  # noqa: F811
    from core.state import store
    server, read = _module(fleet, monkeypatch, tmp_path)
    try:
        job_id = harness.create_job(fleet, topic="Storyboard restore")
        harness.approve_script_only(fleet, job_id, monkeypatch)
        harness.run_storyboard_task(fleet, job_id, monkeypatch)
        harness.wait_for_status(fleet, job_id, "STORYBOARD_PENDING_APPROVAL")
        harness.submit_storyboard_previews(fleet, job_id, count=2)
        harness.wait_for_status(fleet, job_id, "STORYBOARD_READY")
        revision = "Більше крупних планів"
        revised = fleet.post(f"/api/jobs/{job_id}/storyboards/regenerate",
                             json={"version": 1, "actor": "editor", "revision": revision})
        assert revised.status_code == 200, revised.text
        harness.run_storyboard_task(fleet, job_id, monkeypatch, expect_revision=revision)
        harness.wait_for_status(fleet, job_id, "STORYBOARD_PENDING_APPROVAL")
        harness.submit_storyboard_previews(fleet, job_id, count=2)
        harness.approve_storyboard(fleet, job_id)

        before = read(job_id)
        assert before["job_id"] == job_id and before["active_version"] == 2
        assert [v["version"] for v in before["storyboards"]] == [1, 2]
        assert before["storyboards"][1]["revision_request"] == revision
        assert before["storyboards"][1]["status"] == "approved" and before["storyboards"][1]["decided_by"]
        hashes = _image_hashes(job_id, before)
        assert len(hashes) >= 2

        process, api = backup_process["launch"]({"BACKUP_SOURCES": f"storage:{store.root}"})
        sid = api.post("/snapshots", json={"job_id": job_id}).json()["snapshot_id"]
        token = challenge(api, sid)
        shutil.rmtree(Path(store.root) / job_id)  # the job's storage is lost
        response = api.post(f"/snapshots/{sid}/restore/confirm", params={"token": token})
        assert response.status_code == 200, response.text
        process.terminate()
        process.wait(timeout=15)

        restarted = harness.restart_store()  # CORE restarts over the restored storage
        with store.lock:
            store.jobs.clear()
            store.jobs.update(restarted.jobs)
        after = read(job_id)
        # Interrupted asset work is requeued by CORE's restart recovery (job status NEW);
        # every storyboard field, version, scene, reference and approval is unchanged.
        assert {k: v for k, v in after.items() if k != "status"} == {k: v for k, v in before.items() if k != "status"}
        assert after["status"] in {before["status"], "NEW"}
        assert _image_hashes(job_id, after) == hashes
    finally:
        server.shutdown()
