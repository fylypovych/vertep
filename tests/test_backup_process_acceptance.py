"""#354: actual Backup Service processes, encrypted restore and consumer read-back."""
import base64
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest

from tests.acceptance_provenance import acceptance_provenance

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    pytest.fail("Timed out waiting for disposable process boundary")


def stop_process(process, timeout=30):
    """Stop a disposable service; a graceful shutdown that hangs is killed.

    The crash acceptance kills CORE while a restore mutation is still running, so
    uvicorn can hold the graceful shutdown longer than any fixed short wait.  The
    test needs the process gone (that is the simulated crash), not a specific
    signal transition.
    """
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


@pytest.fixture
def backup_process(tmp_path):
    processes, handles, clients = [], [], []
    config, storage = tmp_path / "config", tmp_path / "storage"
    config.mkdir()
    storage.mkdir()
    env = dict(os.environ, BACKUP_ROOT=str(tmp_path / "backups"),
        BACKUP_CONFIG_ROOT=str(config), BACKUP_STORAGE_ROOT=str(storage), BACKUP_SOURCES="",
        BACKUP_RESTORE_STATE_DIR=str(tmp_path / "restore-state"),
        BACKUP_ENCRYPTION_KEY=base64.b64encode(b"synthetic-backup-key-32-bytes!!!!!"[:32]).decode(),
        BACKUP_PG_DUMP_CMD="", BACKUP_REDIS_DUMP_CMD="", BACKUP_CORE_URL="", CORE_ADDRESS="")

    def launch(overrides=None, port=None, app_name="services.backup_service:app", ready_path="/health", headers=None):
        if port is None:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
        log = (tmp_path / f"backup-process-{len(processes)}.log").open("w", encoding="utf-8")
        handles.append(log)
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", app_name,
            "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT,
            env={**env, **(overrides or {})}, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        api = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30, headers=headers)
        clients.append(api)
        def ready():
            assert process.poll() is None, "Backup process exited; see process log"
            try:
                return api.get(ready_path).status_code == 200
            except httpx.HTTPError:
                return False
        wait_for(ready)
        return process, api
    yield {"launch": launch, "root": tmp_path, "storage": storage, "config": config}
    for process in processes:
        stop_process(process)
    for client in clients:
        client.close()
    for handle in handles:
        handle.close()


def challenge(api, sid):
    response = api.post(f"/snapshots/{sid}/restore")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmation_required"
    return response.json()["confirmation_token"]


def test_real_restore_confirmation_restart_and_tamper(backup_process):
    data = backup_process["storage"] / "record.json"
    original = b'{"version":1,"artifact":"approved-output"}'
    data.write_bytes(original)
    process, api = backup_process["launch"]()
    snapshot = api.post("/snapshots", json={"job_id": "process-proof"}).json()
    sid = snapshot["snapshot_id"]
    token = challenge(api, sid)
    other = api.post("/snapshots", json={"job_id": "other"}).json()["snapshot_id"]
    assert api.post(f"/snapshots/{other}/restore/confirm", params={"token": token}).status_code == 401
    process.terminate()
    process.wait(timeout=15)
    process, api = backup_process["launch"]()
    data.write_bytes(b"mutated after snapshot")
    response = api.post(f"/snapshots/{sid}/restore/confirm", params={"token": token})
    assert response.status_code == 200, response.text
    assert data.read_bytes() == original
    process.terminate()
    process.wait(timeout=15)
    _, api = backup_process["launch"]()
    assert api.get(f"/snapshots/{sid}/restore/progress").json()["status"] == "done"
    assert api.post(f"/snapshots/{sid}/restore/confirm", params={"token": token}).status_code == 401
    token = challenge(api, other)
    archive = backup_process["root"] / "backups" / f"{other}.vtbackup"
    archive.write_bytes(archive.read_bytes() + b"synthetic tampering")
    before = data.read_bytes()
    response = api.post(f"/snapshots/{other}/restore/confirm", params={"token": token})
    assert response.status_code == 409 and data.read_bytes() == before


def test_core_caller_fences_writers_and_unhealthy_restore_cannot_enter_normal(backup_process):
    root = backup_process["root"]
    config = root / "core-config"
    config.mkdir()
    (config / "users.json").write_text("{}", encoding="utf-8")
    (config / "installation.json").write_text('{"completed_at":"2026-01-01"}', encoding="utf-8")
    with socket.socket() as socket_probe:
        socket_probe.bind(("127.0.0.1", 0))
        core_port = socket_probe.getsockname()[1]
    internal_key = "synthetic-backup-internal-key"
    _, service = backup_process["launch"]({"BACKUP_CORE_URL": f"http://127.0.0.1:{core_port}",
                                          "INTERNAL_API_KEY": internal_key})
    core_env = {"CONFIG_ROOT": str(config), "UPDATE_STATE_DIR": str(root / "core-update"),
        "STORAGE_ROOT": str(root / "core-storage"), "JOB_ROOT": str(root / "core-jobs"),
        "SYSTEM_STATE_BACKEND": "file", "BACKUP_URL": str(service.base_url).rstrip("/"),
        "INTERNAL_API_KEY": internal_key, "ADMIN_USER": "admin", "ADMIN_PASSWORD": "backup-process-admin",
        "USERS_JSON": "", "DATABASE_URL": "", "REDIS_URL": "", "CORE_ADDRESS": "",
        "OLLAMA_URL": "http://127.0.0.1:1", "PROMETHEUS_URL": ""}
    auth = "Basic " + base64.b64encode(b"admin:backup-process-admin").decode()
    process, api = backup_process["launch"](core_env, core_port, "core.app:app", "/api/session",
                                          {"Authorization": auth})
    data = backup_process["storage"] / "record.json"
    data.write_bytes(b"approved original record")
    response = api.post("/api/system/backups")
    assert response.status_code == 200, response.text
    sid = response.json()["snapshot_id"]
    state_headers = {"x-vertep-internal-key": internal_key}
    assert api.post("/api/system/state", json={"state": "MAINTENANCE"}).status_code == 403
    response = api.post(f"/api/system/backups/{sid}/restore")
    assert response.status_code == 200, response.text
    token = response.json()["confirmation_token"]
    assert api.get("/api/system/state").json()["state"] == "MAINTENANCE"
    assert api.post("/api/jobs", json={"topic": "must not be written"}).status_code == 423
    assert api.post("/api/workers/heartbeat", json={"node_name": "blocked", "role": "gpu"}).status_code == 423
    data.write_bytes(b"mutated disposable record")
    response = api.post(f"/api/system/backups/{sid}/restore/confirm", params={"token": token})
    # The deliberately unavailable Ollama endpoint makes actual CORE unhealthy.
    # The executor must persist failure/EMERGENCY rather than claim NORMAL.
    assert response.status_code == 502, response.text
    assert data.read_bytes() == b"approved original record"
    assert api.get("/api/system/state").json()["state"] == "EMERGENCY"
    assert api.get(f"/api/system/backups/{sid}/restore/progress").json()["status"] == "error"
    operations = json.loads((root / "restore-state/operations.json").read_text(encoding="utf-8"))
    assert operations[sid]["emergency_confirmed"] is True
    stop_process(process)
    _, restarted = backup_process["launch"](core_env, core_port, "core.app:app", "/api/session",
                                           {"Authorization": auth})
    assert restarted.get("/api/system/state").json()["state"] == "EMERGENCY"


def test_crash_and_competing_process_keep_restore_fence(backup_process):
    root = backup_process["root"]
    data = backup_process["storage"] / "record.json"
    data.write_text("approved before backup", encoding="utf-8")
    dump = root / "database.dump"
    dump_script, restore_script = root / "dump.py", root / "restore-tool.py"
    dump_script.write_text("from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_bytes(b'synthetic external DB transport')", encoding="utf-8")
    restore_script.write_text("from pathlib import Path\nimport sys,time\nroot=Path(sys.argv[1])\n(root/'tool-started').write_text('started')\nwhile not (root/'tool-release').exists(): time.sleep(.1)\n", encoding="utf-8")
    def command(script, arg):
        if os.name == "nt":
            return subprocess.list2cmdline([sys.executable, str(script), str(arg)])
        import shlex
        return shlex.join([sys.executable, str(script), str(arg)])
    overrides = {"BACKUP_PG_DUMP_CMD": command(dump_script, dump), "BACKUP_PG_DUMP_PATH": str(dump),
                 "BACKUP_PG_RESTORE_CMD": command(restore_script, root)}
    process, api = backup_process["launch"](overrides)
    sid = api.post("/snapshots", json={"job_id": "crash-proof"}).json()["snapshot_id"]
    token = challenge(api, sid)
    _, competitor = backup_process["launch"](overrides)
    competitor_token = challenge(competitor, sid)
    data.write_text("mutation", encoding="utf-8")
    with concurrent.futures.ThreadPoolExecutor() as pool:
        running = pool.submit(api.post, f"/snapshots/{sid}/restore/confirm", params={"token": token})
        try:
            wait_for(lambda: (root / "tool-started").exists())
            guard_path = root / "restore-state/restore.guard"
            guard = json.loads(guard_path.read_text(encoding="utf-8"))
            guard["started_at"] = 0  # TTL expiry must not steal this live mutation.
            guard_path.write_text(json.dumps(guard), encoding="utf-8")
            assert data.read_text(encoding="utf-8") == "approved before backup"
            response = competitor.post(f"/snapshots/{sid}/restore/confirm", params={"token": competitor_token})
            assert response.status_code == 409, response.text
            assert competitor.get(f"/snapshots/{sid}/restore/progress").json()["status"] == "running"
            stop_process(process)
        finally:
            (root / "tool-release").write_text("release own test transport", encoding="utf-8")
        with pytest.raises(httpx.HTTPError):
            running.result(timeout=15)
    _, restarted = backup_process["launch"](overrides)
    progress = restarted.get(f"/snapshots/{sid}/restore/progress").json()
    assert progress["status"] == "error"
    operation = json.loads((root / "restore-state" / "operations.json").read_text())[sid]
    assert operation["emergency_confirmed"] is False
    assert "CORE" in operation["emergency_error"]
    token = challenge(restarted, sid)
    assert restarted.post(f"/snapshots/{sid}/restore/confirm", params={"token": token}).status_code == 409


def test_approved_media_consumer_readback_after_actual_restore_and_restart(backup_process, fleet, monkeypatch):
    from tests import media_harness as harness
    from tests.test_full_media_harness import _run_to_video
    from core.state import store
    job_id = harness.create_job(fleet)
    _run_to_video(fleet, monkeypatch, job_id)
    harness.approve_video(fleet, job_id)
    harness.register_publisher(fleet)
    assert fleet.post(f"/api/jobs/{job_id}/publish", json=["youtube"]).status_code == 200
    task = harness.claim_publish(fleet, job_id, channel="youtube")
    harness.submit_publish_receipt(fleet, job_id, task, harness.sandbox_receipt("youtube", task))
    job = harness.wait_for_status(fleet, job_id, "PUBLISHED")
    boards = fleet.get(f"/api/jobs/{job_id}/storyboards").json()
    video = harness.video_evidence(job_id, job)
    files = {Path(path): Path(path).read_bytes() for path in video["files"]}
    process, api = backup_process["launch"]({"BACKUP_SOURCES": f"storage:{store.root}"})
    sid = api.post("/snapshots", json={"job_id": job_id}).json()["snapshot_id"]
    token = challenge(api, sid)
    for path in files:
        path.write_bytes(b"mutated own disposable render")
    response = api.post(f"/snapshots/{sid}/restore/confirm", params={"token": token})
    assert response.status_code == 200, response.text
    process.terminate()
    process.wait(timeout=15)
    backup_process["launch"]({"BACKUP_SOURCES": f"storage:{store.root}"})
    restored = harness.restart_store().jobs[job_id]
    assert restored.status.value == "PUBLISHED"
    assert restored.active_video_version == job["active_video_version"]
    assert restored.publication_results == job["publication_results"]
    assert restored.published_to == job["published_to"]
    consumer_config = backup_process["root"] / "consumer-config"
    consumer_config.mkdir()
    (consumer_config / "users.json").write_text("{}", encoding="utf-8")
    (consumer_config / "installation.json").write_text('{"completed_at":"2026-01-01"}', encoding="utf-8")
    auth = "Basic " + base64.b64encode(b"admin:backup-consumer-admin").decode()
    _, consumer = backup_process["launch"]({"CONFIG_ROOT": str(consumer_config),
        "JOB_ROOT": str(store.root), "STORAGE_ROOT": str(store.root.parent),
        "UPDATE_STATE_DIR": str(backup_process["root"] / "consumer-update"),
        "ADMIN_USER": "admin", "ADMIN_PASSWORD": "backup-consumer-admin", "USERS_JSON": "",
        "DATABASE_URL": "", "REDIS_URL": "", "SYSTEM_STATE_BACKEND": "file"},
        app_name="core.app:app", ready_path="/api/session", headers={"Authorization": auth})
    response = consumer.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200, response.text
    consumer_job = response.json()
    for field in ("active_video_version", "publication_results", "published_to", "video_versions"):
        assert consumer_job[field] == job[field]
    assert consumer.get(f"/api/jobs/{job_id}/storyboards").json() == boards
    for path, original in files.items():
        assert path.read_bytes() == original
        assert hashlib.sha256(path.read_bytes()).hexdigest() == video["files"][str(path)]


from tests.test_full_media_harness import client, fleet
