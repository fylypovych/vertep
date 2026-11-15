"""i.0.0.4.77 (#477): artifact hashing and uploads do not block the CORE event loop.

Slow disk I/O is simulated by a controlled gate: the blocked operation waits on
an event while another API request must still complete; then the gate opens and
the first request finishes with an honest result.
"""

import hashlib
import io
import json
import threading
import time
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import core.api.jobs as jobs_api
import core.api.settings as settings_api
import core.api.system as system_api
from core.app import app
from core.state import store


def _artifact(path, data, *, task_id="task-1", size=None, sha256=None, kind="image"):
    return SimpleNamespace(path=path, task_id=task_id, kind=kind, size=len(data) if size is None else size,
                           sha256=sha256 or hashlib.sha256(data).hexdigest())


def _store(tmp_path, artifacts, files):
    job_dir = tmp_path / "job-1"
    (job_dir / "images").mkdir(parents=True)
    for name, data in files.items():
        (job_dir / name).write_bytes(data)
    job = SimpleNamespace(job_id="job-1", artifacts=artifacts, script=None)
    repository = SimpleNamespace(completed_tasks=lambda: [])
    return SimpleNamespace(root=tmp_path, repository=repository), [job]


class TestHonestVerification:
    def test_matching_artifact_is_verified(self, tmp_path):
        data = b"\x89PNG" + b"x" * 100
        fake, jobs = _store(tmp_path, [_artifact("images/a.png", data)], {"images/a.png": data})
        [item] = system_api._verified_task_artifacts(fake, jobs)
        assert item == {"job_id": "job-1", "task_id": "task-1", "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data), "kind": "image"}

    @pytest.mark.parametrize("case", ["corrupt", "size", "missing", "outside", "no-task"])
    def test_unverifiable_artifacts_are_not_counted(self, tmp_path, case):
        data = b"payload" * 10
        stored = {"images/a.png": b"tampered" * 10 if case == "corrupt" else data}
        path = {"missing": "images/gone.png", "outside": "../escape.png"}.get(case, "images/a.png")
        if case == "outside":
            (tmp_path / "escape.png").write_bytes(data)
        artifact = _artifact(path, data, task_id=None if case == "no-task" else "task-1",
                             size=len(data) + 1 if case == "size" else None)
        fake, jobs = _store(tmp_path, [artifact], stored)
        assert system_api._verified_task_artifacts(fake, jobs) == []

    def test_large_file_is_hashed_in_chunks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(system_api, "_HASH_CHUNK_BYTES", 4096)
        data = bytes(range(256)) * 1000  # 256000 bytes
        path = tmp_path / "big.bin"
        path.write_bytes(data)
        reads = []
        real_open = type(path).open

        def tracking_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            original = handle.read
            handle.read = lambda size=-1: reads.append(size) or original(size)
            return handle

        monkeypatch.setattr(type(path), "open", tracking_open)
        assert system_api._stream_digest(path) == (hashlib.sha256(data).hexdigest(), len(data))
        assert reads and set(reads) == {4096}  # never one whole-file read


class _Gate:
    """Blocks the wrapped operation until opened; records that it was entered."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wrap(self, function):
        def blocked(*args, **kwargs):
            self.entered.set()
            assert self.release.wait(10), "gate was never opened"
            return function(*args, **kwargs)
        return blocked


@pytest.fixture
def client():
    """One client = one event loop for every request (a shared anyio portal)."""
    with TestClient(app) as shared:
        yield shared


def _while_blocked(gate, client, slow_call):
    """Run ``slow_call`` in a thread; meanwhile a request on the same loop must answer."""
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("response", slow_call()))
    worker.start()
    try:
        assert gate.entered.wait(10), "slow operation was never reached"
        started = time.monotonic()
        health = client.get("/api/health")
        assert health.status_code == 200
        assert time.monotonic() - started < 5
        assert worker.is_alive(), "the slow request must still be blocked"
    finally:
        gate.release.set()
        worker.join(15)
    return outcome["response"]


def test_system_test_hashing_leaves_the_api_responsive(tmp_path, monkeypatch, client):
    created = client.post("/api/jobs", json={"topic": "artifact hashing"}).json()
    job = store.jobs[created["job_id"]]
    data = b"\x89PNG\r\n\x1a\n" + b"y" * 2048
    target = store.root / job.job_id / "images" / "verified.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    job.artifacts.append(SimpleNamespace(path="images/verified.png", task_id="t-verified", kind="image",
                                         size=len(data), sha256=hashlib.sha256(data).hexdigest()))
    gate = _Gate()
    monkeypatch.setattr(system_api, "_stream_digest", gate.wrap(system_api._stream_digest))
    try:
        response = _while_blocked(gate, client, lambda: client.post("/api/system/test", json={"scope": "full"}))
    finally:
        job.artifacts.pop()
    assert response.status_code == 200, response.text
    verified = response.json()["task_results"]["verified_artifacts"]
    assert {"job_id": job.job_id, "task_id": "t-verified", "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "kind": "image"} in verified


def test_upload_write_leaves_the_api_responsive(monkeypatch, client):
    job_id = client.post("/api/jobs", json={"topic": "upload"}).json()["job_id"]
    gate = _Gate()
    monkeypatch.setattr(jobs_api, "register_artifact", gate.wrap(jobs_api.register_artifact))
    response = _while_blocked(gate, client, lambda: client.put(
        f"/api/jobs/{job_id}/uploads/references/note.txt", content=b"reference text"))
    assert response.status_code == 200, response.text
    assert (store.root / job_id / "references" / "note.txt").read_bytes() == b"reference text"


def test_import_extraction_leaves_the_api_responsive(monkeypatch, client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("job.json", json.dumps({"topic": "imported", "character_id": "did_samogon",
                                                  "priority": 5}))
        archive.writestr("references/a.txt", "data")
    gate = _Gate()
    monkeypatch.setattr(jobs_api, "write_manifest", gate.wrap(jobs_api.write_manifest))
    response = _while_blocked(gate, client, lambda: client.post(
        "/api/projects/import", content=buffer.getvalue(), headers={"content-type": "application/zip"}))
    assert response.status_code == 200, response.text
    assert (store.root / response.json()["job_id"] / "references/a.txt").read_text() == "data"


def test_logo_write_leaves_the_api_responsive(monkeypatch, tmp_path, client):
    monkeypatch.setattr(settings_api, "config_root", lambda: tmp_path)
    gate = _Gate()
    real_to_thread = settings_api.asyncio.to_thread

    async def gated_to_thread(function, *args, **kwargs):
        return await real_to_thread(gate.wrap(function), *args, **kwargs)

    monkeypatch.setattr(settings_api.asyncio, "to_thread", gated_to_thread)
    png = b"\x89PNG\r\n\x1a\n" + b"z" * 64
    response = _while_blocked(gate, client, lambda: client.put(
        "/api/settings/logo", content=png, headers={"content-type": "image/png"}))
    assert response.status_code == 200
    assert (tmp_path / "dashboard-logo.png").read_bytes() == png


def test_storage_probe_leaves_the_api_responsive(monkeypatch, client):
    """audit-1511 В3: the write/fsync/delete storage probe runs off the event loop."""
    gate = _Gate()
    monkeypatch.setattr(system_api, "_storage_probe", gate.wrap(system_api._storage_probe))
    response = _while_blocked(gate, client, lambda: client.post("/api/system/test", json={"scope": "full"}))
    assert response.status_code == 200, response.text
    assert response.json()["storage"]["writable"] is True
    assert response.json()["storage"]["execution_probe"] == "write-fsync-delete"


def test_storage_probe_failure_is_reported(monkeypatch):
    def fail(_root):
        raise OSError("read-only file system")

    monkeypatch.setattr(system_api, "_storage_probe", fail)
    body = TestClient(app).post("/api/system/test", json={"scope": "full"}).json()
    assert "read-only file system" in body["storage_error"]
    assert "storage" not in body or body["storage"].get("writable") is not True
