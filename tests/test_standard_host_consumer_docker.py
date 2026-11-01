"""#387 F1/P1: the standard bash host consumer drives a real Docker runtime.

Unlike ``disposable_deployment`` (a Python stand-in consumer), these tests copy the
shipped ``scripts/vertep`` into a disposable Worker installation and let the real
update agent run it end to end: a signed manifest is fetched over HTTPS and verified,
the bash consumer backs up, migrates the legacy flat layout, activates the release,
rewrites the image pins and recreates the ``worker`` container through Docker
Compose.  Every assertion is made against the running container itself.

They need Docker and bash; CI runs them in the ``fleet-persistence-runtime-acceptance``
job with ``VERTEP_REQUIRE_STANDARD_HOST_CONSUMER=1`` so a missing daemon fails instead of
skipping.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = os.getenv("VERTEP_REQUIRE_STANDARD_HOST_CONSUMER") == "1"
# Two different images, as two releases ship different digests in production.
IMAGES = {"0.0.0.1": "python:3.12-alpine", "0.0.0.2": "python:3.13-alpine"}
CONSUMER_SCRIPTS = ("vertep", "release-layout.py", "safe-extract.py", "update-runtime-env.py")
COMPOSE = """name: ${VERTEP_CONSUMER_PROJECT}
services:
  worker:
    image: ${VERTEP_WORKER_IMAGE}
    command: ["python", "-c", "import time; time.sleep(10**9)"]
    volumes:
      - ./:/release:ro
      - ./storage:/data/storage:ro
"""


def _usable() -> bool:
    if os.name != "posix" or shutil.which("bash") is None or shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    not _usable() and not REQUIRED, reason="standard host consumer needs bash and Docker")


@pytest.fixture(autouse=True)
def _require_docker():
    if REQUIRED and not _usable():
        pytest.fail("VERTEP_REQUIRE_STANDARD_HOST_CONSUMER=1 but bash/Docker is unavailable")


def _run(*command, **kwargs) -> str:
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, f"{command}: {result.stdout}{result.stderr}"
    return result.stdout.strip()


def _pinned(image: str) -> dict:
    _run("docker", "pull", "-q", image)
    repo_digest = _run("docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", image)
    return {"reference": image, "digest": repo_digest.split("@", 1)[1]}


def _payload(base: Path, version: str, image: dict) -> Path:
    """A release payload with the files the standard consumer reads."""
    payload = base / f"vertep-{version}"
    (payload / "scripts").mkdir(parents=True)
    (payload / "core").mkdir()
    (payload / "core" / "__init__.py").write_text("", encoding="utf-8")
    (payload / "config").mkdir()
    for name in CONSUMER_SCRIPTS:
        shutil.copyfile(ROOT / "scripts" / name, payload / "scripts" / name)
    shutil.copyfile(ROOT / "config" / "node_roles.json", payload / "config" / "node_roles.json")
    (payload / "VERSION").write_text(version + "\n", encoding="utf-8")
    (payload / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    # A signed release always pins the image of the selected GPU service as well;
    # update-runtime-env.py refuses a manifest without it.
    (payload / "manifest.json").write_text(
        json.dumps({"version": version, "images": {"worker": image, "comfyui": image}}),
        encoding="utf-8")
    return payload


def _archive(payload: Path) -> Path:
    archive = payload.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as package:
        package.add(payload, arcname=payload.name)
    return archive


def _records_digest(root: Path) -> str:
    return hashlib.sha256((root / "storage" / "records.json").read_bytes()).hexdigest()


class Installation:
    def __init__(self, root: Path, project: str):
        self.root, self.project = root, project

    def compose(self, *arguments: str) -> str:
        release = self.root / "current" if (self.root / "current").is_symlink() else self.root
        return _run("docker", "compose", "--env-file", str(self.root / ".env"),
                    "-f", str(release / "docker-compose.yml"), *arguments)

    def container(self) -> str:
        return _run("docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}",
                    "--filter", "label=com.docker.compose.service=worker", "--filter", "status=running")

    def served_version(self, container: str) -> str:
        return _run("docker", "exec", container, "cat", "/release/VERSION")

    def consumer_records(self, container: str) -> str:
        data = _run("docker", "exec", container, "cat", "/data/storage/records.json")
        return hashlib.sha256((data + "\n").encode()).hexdigest()


@pytest.fixture
def installation(tmp_path):
    """A legacy flat Worker installation running release 0.0.0.1 in Docker."""
    project = f"vertep-consumer-{uuid.uuid4().hex[:10]}"
    root = tmp_path / "opt-vertep"
    image = _pinned(IMAGES["0.0.0.1"])
    flat = _payload(tmp_path / "seed", "0.0.0.1", image)
    shutil.copytree(flat, root)
    (root / "storage").mkdir()
    (root / "storage" / "records.json").write_text('{"approved": ["job-1", "job-2"]}\n',
                                                  encoding="utf-8")
    (root / ".env").write_text(
        f"ROLE=worker\nVERTEP_CONSUMER_PROJECT={project}\nVERTEP_VERSION=0.0.0.1\n"
        f"VERTEP_WORKER_IMAGE={image['reference']}@{image['digest']}\n", encoding="utf-8")
    current = Installation(root, project)
    current.compose("up", "-d", "worker")
    yield current
    subprocess.run(["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"],
                   capture_output=True)


def _agent():
    spec = importlib.util.spec_from_file_location("update_agent_consumer", ROOT / "scripts" / "update-agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(state_dir: Path, request_id: str, action: str, target: str | None = None) -> Path:
    requests = state_dir / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    path = requests / f"{request_id}.json"
    body = {"request_id": request_id, "action": action}
    if target:
        body["target_version"] = target
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _status(state_dir: Path) -> dict:
    return json.loads((state_dir / "status.json").read_text(encoding="utf-8"))


def _explain(state_dir: Path, installation=None) -> str:
    """Full executor outcome for an assertion message (pytest truncates dict reprs)."""
    status = _status(state_dir)
    lines = [f"state={status.get('state')} phase={status.get('phase')}",
             f"message={status.get('message')}", "log:", *status.get("log", [])[-60:]]
    if installation is not None:
        lines.append(subprocess.run(
            ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={installation.project}",
             "--format", "{{.ID}} {{.Image}} {{.Status}}"], capture_output=True, text=True).stdout)
    return "\n".join(str(line) for line in lines)


def test_standard_consumer_updates_restarts_and_rolls_back_a_running_worker(
        installation, signed_update_server, tmp_path, monkeypatch):
    root = installation.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    monkeypatch.setenv("UPDATE_FENCE_CHECK_SECONDS", "0.5")
    agent = _agent()

    first = installation.container()
    assert first and installation.served_version(first) == "0.0.0.1"
    records = installation.consumer_records(first)
    assert records == _records_digest(root)

    # update: signed manifest → bash consumer → legacy rescue → activation → recreate
    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    update_id = "1" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, update_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    status = _status(state_dir)
    assert status["state"] == "SUCCEEDED" and status["request_id"] == update_id
    updated = installation.container()
    assert updated and updated != first, "the running worker was not recreated"
    assert installation.served_version(updated) == "0.0.0.2"
    assert IMAGES["0.0.0.2"] in _run("docker", "inspect", "--format", "{{.Config.Image}}", updated)
    assert os.readlink(root / "current") == os.path.join("releases", "0.0.0.2")
    assert (root / "releases" / "0.0.0.1" / "VERSION").is_file(), "legacy layout was not rescued"
    assert installation.consumer_records(updated) == records == _records_digest(root)

    # restart: the same release, a new running instance, correlated with the request
    restart_id = "2" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, restart_id, "restart"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    assert _status(state_dir)["request_id"] == restart_id
    restarted = installation.container()
    assert restarted and restarted != updated
    assert installation.served_version(restarted) == "0.0.0.2"

    # rollback: the previous immutable release and its image are running again
    rollback_id = "3" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, rollback_id, "rollback"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    status = _status(state_dir)
    assert status["state"] == "ROLLED_BACK" and status["request_id"] == rollback_id
    restored = installation.container()
    assert restored and restored != restarted
    assert installation.served_version(restored) == "0.0.0.1"
    assert IMAGES["0.0.0.1"] in _run("docker", "inspect", "--format", "{{.Config.Image}}", restored)
    assert installation.consumer_records(restored) == records == _records_digest(root)

    journal = [json.loads(line) for line in
               (state_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {event.get("operation_id") for event in journal} >= {update_id, restart_id, rollback_id}


def test_standard_consumer_refuses_a_tampered_release_without_touching_the_worker(
        installation, signed_update_server, tmp_path, monkeypatch):
    root = installation.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()
    before = installation.container()

    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))), tamper=True)
    request_id = "4" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is False, _explain(state_dir, installation)
    status = _status(state_dir)
    assert status["state"] == "FAILED" and status["request_id"] == request_id, _explain(state_dir, installation)
    assert "signature" in status["message"].lower()
    assert installation.container() == before
    assert installation.served_version(before) == "0.0.0.1"
    assert not (root / "current").exists() and not (root / "releases").exists()


def test_failed_legacy_rescue_blocks_the_destructive_recreate(
        installation, signed_update_server, tmp_path, monkeypatch):
    """#387 P1: when the legacy layout cannot be migrated into an immutable release
    (here: a symlink the release digest refuses), the consumer stops before
    recreating the worker; the running instance and its data stay untouched."""
    root = installation.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()
    before = installation.container()
    records = installation.consumer_records(before)
    (root / "legacy-link").symlink_to("VERSION")

    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    request_id = "5" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    status = _status(state_dir)
    assert status["request_id"] == request_id
    # The refusal must come from the legacy rescue, not from an unrelated failure.
    assert any("symlink" in str(line) for line in status.get("log", [])), _explain(state_dir, installation)
    assert installation.container() == before, "a failed rescue still recreated the worker"
    assert installation.served_version(before) == "0.0.0.1"
    assert installation.consumer_records(before) == records == _records_digest(root)
    assert not (root / "releases" / "0.0.0.2").exists()


def _consumer_tree(container: str) -> dict[str, str]:
    """sha256 of every file the running consumer sees below /data/storage."""
    listing = _run("docker", "exec", container, "sh", "-c",
                   "cd /data/storage && find . -type f | sort | xargs -r sha256sum")
    return {name.removeprefix("./"): digest for digest, name in
            (line.split(None, 1) for line in listing.splitlines() if line.strip())}


def _host_tree(storage: Path) -> dict[str, str]:
    return {path.relative_to(storage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(storage.rglob("*")) if path.is_file()}


@pytest.fixture
def backup_service(installation, tmp_path):
    """The real Backup Service process protecting the installation's data roots."""
    import base64
    import socket
    import sys
    import time

    import httpx

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(os.environ, BACKUP_ROOT=str(tmp_path / "native-backups"),
               BACKUP_CONFIG_ROOT=str(installation.root / "config"),
               BACKUP_STORAGE_ROOT=str(installation.root / "storage"), BACKUP_SOURCES="",
               BACKUP_RESTORE_STATE_DIR=str(tmp_path / "restore-state"),
               BACKUP_ENCRYPTION_KEY=base64.b64encode(b"consumer-backup-key-32-bytes!!!!"[:32]).decode(),
               BACKUP_PG_DUMP_CMD="", BACKUP_REDIS_DUMP_CMD="", BACKUP_CORE_URL="", CORE_ADDRESS="")
    log = (tmp_path / "backup-service.log").open("w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "services.backup_service:app",
                                "--host", "127.0.0.1", "--port", str(port)],
                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)
    deadline = time.monotonic() + 60
    while True:
        assert process.poll() is None, (tmp_path / "backup-service.log").read_text(encoding="utf-8")
        try:
            if client.get("/health").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, "Backup Service did not become healthy"
        time.sleep(0.2)
    yield client
    client.close()
    process.terminate()
    process.wait(timeout=15)
    log.close()


def test_native_restore_is_read_back_by_the_consumer_across_update_restart_and_rollback(
        installation, backup_service, signed_update_server, tmp_path, monkeypatch):
    """#387 P4: approvals, receipts and mappings snapshotted by the real Backup
    Service are lost after an update, restored, and read back by the running worker
    after the standard update, a restart and a rollback."""
    root = installation.root
    storage = root / "storage"
    (storage / "receipts").mkdir()
    (storage / "receipts" / "job-1.json").write_text('{"job": "job-1", "receipt": "pub-1"}\n',
                                                     encoding="utf-8")
    (storage / "mappings.json").write_text('{"job-1": "upstream-7"}\n', encoding="utf-8")
    approved = _host_tree(storage)
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    snapshot = backup_service.post("/snapshots", json={"job_id": "consumer-proof"})
    assert snapshot.status_code == 200, snapshot.text
    snapshot_id = snapshot.json()["snapshot_id"]

    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    assert agent.process_request(root, state_dir, _request(state_dir, "6" * 32, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, installation)

    # Data loss after the update: an approval and a receipt disappear.
    (storage / "records.json").write_text('{"approved": []}\n', encoding="utf-8")
    (storage / "receipts" / "job-1.json").unlink()
    assert _consumer_tree(installation.container()) != approved

    challenge = backup_service.post(f"/snapshots/{snapshot_id}/restore")
    assert challenge.status_code == 200, challenge.text
    restored = backup_service.post(f"/snapshots/{snapshot_id}/restore/confirm",
                                   params={"token": challenge.json()["confirmation_token"]})
    assert restored.status_code == 200, restored.text
    assert backup_service.get(f"/snapshots/{snapshot_id}/restore/progress").json()["status"] == "done"

    updated = installation.container()
    assert installation.served_version(updated) == "0.0.0.2"
    assert _consumer_tree(updated) == approved == _host_tree(storage)

    assert agent.process_request(root, state_dir, _request(state_dir, "7" * 32, "restart"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    restarted = installation.container()
    assert restarted != updated and _consumer_tree(restarted) == approved

    assert agent.process_request(root, state_dir, _request(state_dir, "8" * 32, "rollback"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    rolled_back = installation.container()
    assert installation.served_version(rolled_back) == "0.0.0.1"
    assert _consumer_tree(rolled_back) == approved
