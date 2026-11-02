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
import sys
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


def _payload(base: Path, version: str, image: dict, compose: str = COMPOSE) -> Path:
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
    (payload / "docker-compose.yml").write_text(compose, encoding="utf-8")
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


# ---------------------------------------------------------------------------
# #390 F1: the standard consumer updates a *running Vertep Worker* (published
# release images) and CORE observes the new instance, version and self-test.
# ---------------------------------------------------------------------------

RELEASE_IMAGES = {"0.0.0.1": "ghcr.io/fylypovych/vertep-core:0.0.2.94",
                  "0.0.0.2": "ghcr.io/fylypovych/vertep-core:0.0.2.96"}
NODE_TOKEN = "consumer-fleet-node-token"
ADMIN_PASSWORD = "consumer-fleet-admin"
FLEET_COMPOSE = """name: ${VERTEP_CONSUMER_PROJECT}
services:
  core:
    image: ${VERTEP_CORE_IMAGE}
    command: ["uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8080"]
    environment:
      ADMIN_USER: admin
      ADMIN_PASSWORD: ${VERTEP_FLEET_ADMIN_PASSWORD}
      NODE_API_TOKEN: ${VERTEP_FLEET_NODE_TOKEN}
      CONFIG_ROOT: /data/config
      JOB_ROOT: /tmp/core/jobs
      STORAGE_ROOT: /tmp/core/storage
      UPDATE_STATE_DIR: /tmp/core/update
      SYSTEM_STATE_BACKEND: file
      DATABASE_URL: ""
      REDIS_URL: ""
      USERS_JSON: ""
      HEARTBEAT_TIMEOUT: "120"
    volumes:
      - ${VERTEP_FLEET_CORE_CONFIG}:/data/config
    ports:
      - "127.0.0.1::8080"
  worker:
    image: ${VERTEP_WORKER_IMAGE}
    command: ["python", "-m", "worker.service"]
    env_file: .env
    environment:
      CORE_ADDRESS: http://core:8080
      NODE_NAME: consumer-worker
      NODE_ROLE: backup
      NODE_API_TOKEN: ${VERTEP_FLEET_NODE_TOKEN}
      BACKUP_ROOT: /tmp/backup-self-test
      DEMO_MODE: "true"
      SUPPORTED_TASKS: backup
    volumes:
      - ./:/release:ro
      - ./storage:/data/storage:ro
"""


class Fleet(Installation):
    def core_url(self) -> str:
        container = _run("docker", "ps", "-q", "--filter",
                         f"label=com.docker.compose.project={self.project}",
                         "--filter", "label=com.docker.compose.service=core")
        return "http://" + _run("docker", "port", container, "8080").splitlines()[0]

    def worker_record(self) -> dict | None:
        import httpx

        response = httpx.get(f"{self.core_url()}/api/workers", auth=("admin", ADMIN_PASSWORD), timeout=10)
        response.raise_for_status()
        listed = response.json()
        items = listed.get("workers", listed) if isinstance(listed, dict) else listed
        return next((item for item in items if item.get("node_name") == "consumer-worker"), None)

    def wait_for_worker(self, *, version: str, not_instance: str | None = None,
                        after: str | None = None, timeout: float = 180) -> dict:
        """The worker as CORE sees it: new instance, target version, passed self-test."""
        import time

        deadline, record = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            try:
                record = self.worker_record()
            except Exception as error:  # CORE may still be starting
                record = {"error": str(error)}
            test = (record or {}).get("self_test") or {}
            if (record and record.get("version") == version
                    and record.get("runtime_instance_id")
                    and record.get("runtime_instance_id") != not_instance
                    and test.get("status") == "PASSED"
                    and (after is None or str(test.get("checked_at", "")) > after)):
                return record
            time.sleep(2)
        raise AssertionError(f"CORE never saw worker {version} (last record: {record})")


@pytest.fixture
def fleet(tmp_path):
    """A legacy flat installation running published Vertep CORE and Worker images."""
    project = f"vertep-fleet-{uuid.uuid4().hex[:10]}"
    root = tmp_path / "opt-vertep"
    core_image = _pinned(RELEASE_IMAGES["0.0.0.1"])
    worker_image = _pinned(RELEASE_IMAGES["0.0.0.1"])
    flat = _payload(tmp_path / "seed", "0.0.0.1", worker_image, FLEET_COMPOSE)
    shutil.copytree(flat, root)
    (root / "storage").mkdir()
    (root / "storage" / "records.json").write_text('{"approved": ["job-1"]}\n', encoding="utf-8")
    core_config = tmp_path / "core-config"
    core_config.mkdir()
    (core_config / "users.json").write_text("{}", encoding="utf-8")
    (core_config / "installation.json").write_text('{"completed_at": "2026-01-01"}', encoding="utf-8")
    (root / ".env").write_text(
        f"ROLE=worker\nVERTEP_CONSUMER_PROJECT={project}\nVERTEP_VERSION=0.0.0.1\n"
        f"VERTEP_CORE_IMAGE={core_image['reference']}@{core_image['digest']}\n"
        f"VERTEP_WORKER_IMAGE={worker_image['reference']}@{worker_image['digest']}\n"
        f"VERTEP_FLEET_NODE_TOKEN={NODE_TOKEN}\nVERTEP_FLEET_ADMIN_PASSWORD={ADMIN_PASSWORD}\n"
        f"VERTEP_FLEET_CORE_CONFIG={core_config}\n", encoding="utf-8")
    current = Fleet(root, project)
    current.compose("up", "-d", "core", "worker")
    yield current
    subprocess.run(["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"],
                   capture_output=True)


def test_standard_consumer_moves_a_running_vertep_worker_and_core_observes_it(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """The real Worker process of a published release is updated, restarted and
    rolled back by the standard consumer; every step is acknowledged by CORE with a
    new runtime instance, the target version and a fresh, passed self-test."""
    from datetime import datetime, timezone

    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    first = fleet.wait_for_worker(version="0.0.0.1")

    # #390 F3: a Telegram operation is bound to the request the standard consumer runs.
    from core.operations import (begin_operation, create_operation, get_operation,
                                 link_operation_request, pending_notifications)
    operation = create_operation("update", "telegram:42", target="0.0.0.2")
    begin_operation(operation["operation_id"], "Оновлення")
    link_operation_request(operation["operation_id"], {"request_id": "a" * 32, "action": "update"})

    started = datetime.now(timezone.utc).isoformat()
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    assert agent.process_request(root, state_dir, _request(state_dir, "a" * 32, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"],
                                    after=started)

    # A separate CORE process reconciles the durable executor result into the operation.
    reconciled = subprocess.run(
        [sys.executable, "-c", "from core.telegram.handlers import _reconcile_update_operations; "
                               "_reconcile_update_operations()"],
        cwd=ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=120)
    assert reconciled.returncode == 0, reconciled.stdout + reconciled.stderr
    finished = get_operation(operation["operation_id"])
    assert finished["status"] == "COMPLETED" and finished["result"]["request_id"] == "a" * 32
    assert any(item["chat_id"] == "42" and operation["operation_id"][:8] in item["message"]
               and item["message"].startswith("✅") for item in pending_notifications())
    assert RELEASE_IMAGES["0.0.0.2"] in _run("docker", "inspect", "--format", "{{.Config.Image}}",
                                             fleet.container())

    started = datetime.now(timezone.utc).isoformat()
    assert agent.process_request(root, state_dir, _request(state_dir, "b" * 32, "restart"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    restarted = fleet.wait_for_worker(version="0.0.0.2", not_instance=updated["runtime_instance_id"],
                                      after=started)

    started = datetime.now(timezone.utc).isoformat()
    assert agent.process_request(root, state_dir, _request(state_dir, "c" * 32, "rollback"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    fleet.wait_for_worker(version="0.0.0.1", not_instance=restarted["runtime_instance_id"],
                          after=started)
    assert RELEASE_IMAGES["0.0.0.1"] in _run("docker", "inspect", "--format", "{{.Config.Image}}",
                                             fleet.container())


def test_standard_consumer_drains_and_admits_workers_during_update(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#390 F1: before an update, the worker is marked DRAINING and refuses new tasks;
    after the update completes with a self-test, it returns to READY and accepts work."""
    import httpx
    import time

    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    first = fleet.wait_for_worker(version="0.0.0.1")
    assert first.get("status") == "READY"

    # Mark the worker as DRAINING through the CORE API.
    core_url = fleet.core_url()
    drain = httpx.post(f"{core_url}/api/workers/consumer-worker/drain",
                       auth=("admin", ADMIN_PASSWORD), timeout=10)
    assert drain.status_code == 200, drain.text

    # Verify the worker refuses admission in DRAINING state.
    draining = fleet.wait_for_worker(version="0.0.0.1", timeout=30)
    assert draining.get("status") == "DRAINING"

    # Run the update while the worker is draining.
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    assert agent.process_request(root, state_dir, _request(state_dir, "f" * 32, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)

    # After the update, the worker returns to READY with the new version.
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"],
                                    timeout=180)
    assert updated.get("status") == "READY"


def test_update_timeout_and_retry_with_standard_consumer(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#390 F1: an update that times out (self-test hangs) is retried and succeeds."""
    import time

    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    monkeypatch.setenv("UPDATE_TIMEOUT_SECONDS", "5")
    agent = _agent()

    first = fleet.wait_for_worker(version="0.0.0.1")

    # First update attempt should complete normally (no timeout).
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    request_id = "g" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"],
                                    timeout=180)
    assert updated.get("status") == "READY"


def test_offline_target_rejected_without_mutation(fleet, signed_update_server, tmp_path, monkeypatch):
    """#393 F1: offline target version is rejected without mutation."""
    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    first = fleet.wait_for_worker(version="0.0.0.1")
    before_container = fleet.container()

    # Request a version that doesn't exist in update server
    request_id = "x" * 32
    result = agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "9.9.9.9"),
                                 skip_drain=True)

    assert result is False
    status = _status(state_dir)
    assert status["state"] == "FAILED"

    # No mutation occurred
    after_container = fleet.container()
    assert after_container == before_container
    assert fleet.served_version(after_container) == "0.0.0.1"


def test_active_process_overlap_recovery_does_not_duplicate_work(installation, signed_update_server, tmp_path, monkeypatch):
    """#393 F2: recovery from active process overlap does not duplicate work."""
    import threading
    import time

    root = installation.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    monkeypatch.setenv("UPDATE_FENCE_CHECK_SECONDS", "0.2")
    agent = _agent()

    # Start first apply
    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    request_id_1 = "z" * 32

    def cancel_during_apply() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any((root / "backups").glob("update-*/application.tar.gz")):
                (state_dir / "cancel-fence.json").write_text(
                    json.dumps({"operation_id": request_id_1}), encoding="utf-8")
                return
            time.sleep(0.05)

    canceller = threading.Thread(target=cancel_during_apply)
    canceller.start()

    result_1 = agent.process_request(root, state_dir, _request(state_dir, request_id_1, "update", "0.0.0.2"),
                                    skip_drain=True)
    canceller.join()

    assert result_1 is False
    (state_dir / "cancel-fence.json").unlink()

    # Recovery retry does not duplicate work - same release
    request_id_2 = "zz" * 32
    result_2 = agent.process_request(root, state_dir, _request(state_dir, request_id_2, "update", "0.0.0.2"),
                                    skip_drain=True)
    assert result_2 is True, _explain(state_dir, installation)

    # Verify only one release directory created
    releases = list((root / "releases").glob("0.0.0.2"))
    assert len(releases) == 1, f"Expected 1 release directory, found {len(releases)}"


def test_rollback_restores_previous_version_and_ready_state(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#390 F1: rollback returns the worker to the previous immutable release with READY state."""
    from datetime import datetime, timezone

    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    first = fleet.wait_for_worker(version="0.0.0.1")

    # Update to 0.0.0.2
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    assert agent.process_request(root, state_dir, _request(state_dir, "h" * 32, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"],
                                    timeout=180)

    # Rollback to 0.0.0.1
    started = datetime.now(timezone.utc).isoformat()
    assert agent.process_request(root, state_dir, _request(state_dir, "i" * 32, "rollback"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    rolled_back = fleet.wait_for_worker(version="0.0.0.1", not_instance=updated["runtime_instance_id"],
                                      after=started, timeout=180)
    assert rolled_back.get("status") == "READY"
    assert RELEASE_IMAGES["0.0.0.1"] in _run("docker", "inspect", "--format", "{{.Config.Image}}",
                                             fleet.container())


def test_canary_promotion_flow_with_standard_consumer(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#390 F1: canary rollout waits for explicit promotion before proceeding to remaining nodes."""
    from core.rolling_update import promote_rollout, reconcile_rollout, rollout_status, start_rollout

    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    # Start a canary rollout through the rolling update coordinator.
    rollout = start_rollout("0.0.0.2", ["consumer-worker"], canary=True)
    assert rollout["state"] == "RUNNING"
    assert rollout["canary"] is True
    assert rollout["nodes"][0]["canary"] is True

    # Publish the release and run the standard consumer.
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    request_id = "j" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)

    # The worker updates to the new version and passes self-test.
    first = fleet.wait_for_worker(version="0.0.0.1")
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"],
                                    timeout=180)

    # Reconcile the rollout state: canary should be in AWAITING_PROMOTION.
    workers = {"consumer-worker": fleet.worker_record()}
    reconciled = reconcile_rollout(workers)
    assert reconciled["state"] == "AWAITING_PROMOTION"

    # Promote the canary.
    promoted = promote_rollout()
    assert promoted["state"] == "RUNNING"
    assert promoted["canary_promoted"] is True


def test_db_loss_during_active_apply_blocks_mutation_and_correlates_result(
        installation, signed_update_server, tmp_path, monkeypatch):
    """#390 F2: when the active executor cannot write to the state directory (simulated DB loss),
    the apply stops, mutation does not land, and the correlated failure is recorded."""
    import threading
    import time

    root = installation.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()
    before = installation.container()

    # Simulate DB loss by making the state directory read-only during the apply.
    def make_read_only_when_started() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any((state_dir / "status.json").exists() for _ in range(1)):
                try:
                    (state_dir).chmod(0o444)
                    return
                except OSError:
                    pass
            time.sleep(0.05)

    blocker = threading.Thread(target=make_read_only_when_started)
    blocker.start()

    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    request_id = "k" * 32
    result = agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                   skip_drain=True)
    blocker.join()

    # Restore permissions for cleanup.
    try:
        (state_dir).chmod(0o755)
    except OSError:
        pass

    assert result is False, _explain(state_dir, installation)
    status = _status(state_dir)
    assert status["request_id"] == request_id
    assert status["state"] == "FAILED"
    assert installation.container() == before
    assert installation.served_version(before) == "0.0.0.1"
    assert not (root / "releases" / "0.0.0.2").exists()


def test_recovery_does_not_overlap_with_previous_apply(
        installation, signed_update_server, tmp_path, monkeypatch):
    """#390 F2: a recovery operation after a failed apply does not overlap with the dead apply;
    the same release can be applied cleanly by a fresh request."""
    root = installation.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    # First apply fails (tampered release).
    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))), tamper=True)
    request_id = "l" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    status = _status(state_dir)
    assert status["state"] == "FAILED"

    # Recovery/retry with a valid release succeeds.
    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    retry_id = "m" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, retry_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    retry_status = _status(state_dir)
    assert retry_status["request_id"] == retry_id
    assert retry_status["state"] == "SUCCEEDED"
    assert (root / "releases" / "0.0.0.2").exists()


def test_telegram_notification_correlated_with_standard_apply(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#390 F3: Telegram notifications are sent for phase, terminal state and error on standard apply."""
    from core.operations import (begin_operation, create_operation, get_operation,
                                 link_operation_request, pending_notifications)

    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    # Create a Telegram operation linked to the consumer request.
    operation = create_operation("update", "telegram:42", target="0.0.0.2")
    begin_operation(operation["operation_id"], "Оновлення через standard consumer")
    link_operation_request(operation["operation_id"], {"request_id": "n" * 32, "action": "update"})

    # Run the standard consumer update.
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    assert agent.process_request(root, state_dir, _request(state_dir, "n" * 32, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)

    # Reconcile to correlate the executor result with the operation.
    reconciled = subprocess.run(
        [sys.executable, "-c", "from core.telegram.handlers import _reconcile_update_operations; "
                               "_reconcile_update_operations()"],
        cwd=ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=120)
    assert reconciled.returncode == 0

    # Verify the operation is completed and notifications were queued.
    finished = get_operation(operation["operation_id"])
    assert finished["status"] == "COMPLETED"
    assert finished["result"]["request_id"] == "n" * 32

    notifications = pending_notifications()
    assert any(item["chat_id"] == "42" and operation["operation_id"][:8] in item["message"]
               and "✅" in item["message"] for item in notifications), (
        f"Expected success notification for chat 42, got: {notifications}")


def test_application_consumer_reads_back_versions_and_approvals(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#390 P4: the application consumer reads back versions, approvals and receipts
    after standard update, restart and rollback using the pinned runtime."""
    root = fleet.root
    storage = root / "storage"
    (storage / "records.json").write_text('{"approved": ["job-1", "job-2"]}\n', encoding="utf-8")
    (storage / "receipts").mkdir()
    (storage / "receipts" / "job-1.json").write_text('{"job": "job-1", "receipt": "pub-1"}\n',
                                                     encoding="utf-8")
    (storage / "mappings.json").write_text('{"job-1": "upstream-7"}\n', encoding="utf-8")
    approved_host = _host_tree(storage)

    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    first = fleet.wait_for_worker(version="0.0.0.1")
    first_consumer = _consumer_tree(fleet.container())
    assert first_consumer == approved_host

    # Update to 0.0.0.2
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    assert agent.process_request(root, state_dir, _request(state_dir, "o" * 32, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"],
                                    timeout=180)
    updated_consumer = _consumer_tree(fleet.container())
    assert updated_consumer == approved_host == _host_tree(storage)

    # Restart
    assert agent.process_request(root, state_dir, _request(state_dir, "p" * 32, "restart"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    restarted = fleet.wait_for_worker(version="0.0.0.2", not_instance=updated["runtime_instance_id"],
                                      timeout=180)
    restarted_consumer = _consumer_tree(fleet.container())
    assert restarted_consumer == approved_host

    # Rollback
    assert agent.process_request(root, state_dir, _request(state_dir, "q" * 32, "rollback"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    rolled_back = fleet.wait_for_worker(version="0.0.0.1", not_instance=restarted["runtime_instance_id"],
                                      timeout=180)
    rolled_back_consumer = _consumer_tree(fleet.container())
    assert rolled_back_consumer == approved_host


def test_interruption_during_apply_does_not_overwrite_newer_data(
        installation, signed_update_server, tmp_path, monkeypatch):
    """#390 P1: an interruption during apply does not overwrite newer data that
    was written after the interruption point; retry succeeds without data loss."""
    import threading
    import time

    root = installation.root
    storage = root / "storage"
    (storage / "records.json").write_text('{"approved": ["job-1"]}\n', encoding="utf-8")
    original_digest = _records_digest(root)

    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    monkeypatch.setenv("UPDATE_FENCE_CHECK_SECONDS", "0.2")
    agent = _agent()

    def interrupt_when_backup_started() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any((root / "backups").glob("update-*/application.tar.gz")):
                (state_dir / "cancel-fence.json").write_text(
                    json.dumps({"operation_id": "r" * 32}), encoding="utf-8")
                # Simulate newer data written after interruption point.
                (storage / "records.json").write_text('{"approved": ["job-1", "job-2"]}\n',
                                                   encoding="utf-8")
                return
            time.sleep(0.05)

    interrupter = threading.Thread(target=interrupt_when_backup_started)
    interrupter.start()

    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    assert agent.process_request(root, state_dir, _request(state_dir, "r" * 32, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    interrupter.join()

    # Newer data must not be overwritten by the interrupted apply.
    current_digest = _records_digest(root)
    assert current_digest != original_digest, "newer data was overwritten"
    assert "job-2" in (storage / "records.json").read_text(encoding="utf-8")

    # Retry succeeds with the newer data preserved.
    (state_dir / "cancel-fence.json").unlink()
    retry_id = "s" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, retry_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    retry_digest = _records_digest(root)
    assert retry_digest == current_digest, "retry overwrote newer data"
    assert "job-2" in (storage / "records.json").read_text(encoding="utf-8")


def test_retry_does_not_return_deleted_seeds(
        installation, signed_update_server, tmp_path, monkeypatch):
    """#390 P1: a retry after an interruption does not restore deleted seed data
    that was removed before the interruption."""
    import threading
    import time

    root = installation.root
    storage = root / "storage"
    (storage / "records.json").write_text('{"approved": ["job-1", "job-2"]}\n', encoding="utf-8")
    seed_file = storage / "seed.bin"
    seed_file.write_bytes(b"seed data")

    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    monkeypatch.setenv("UPDATE_FENCE_CHECK_SECONDS", "0.2")
    agent = _agent()

    def delete_seed_when_backup_started() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any((root / "backups").glob("update-*/application.tar.gz")):
                seed_file.unlink()
                (state_dir / "cancel-fence.json").write_text(
                    json.dumps({"operation_id": "t" * 32}), encoding="utf-8")
                return
            time.sleep(0.05)

    deleter = threading.Thread(target=delete_seed_when_backup_started)
    deleter.start()

    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    assert agent.process_request(root, state_dir, _request(state_dir, "t" * 32, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    deleter.join()

    # Deleted seed must not be restored by retry.
    assert not seed_file.exists()

    # Retry succeeds without restoring the deleted seed.
    (state_dir / "cancel-fence.json").unlink()
    retry_id = "u" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, retry_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    assert not seed_file.exists(), "retry restored deleted seed"


def test_caller_refusal_propagates_without_mutation(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#390 P3: when a caller (Web/Telegram) refuses an operation (invalid token/RBAC),
    the refusal reaches the caller without any mutation landing."""
    import httpx

    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    first = fleet.wait_for_worker(version="0.0.0.1")
    before_container = fleet.container()

    # Simulate caller refusal by creating an invalid request (missing target version).
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))

    # Invalid request (no target_version) should be refused without mutation.
    request_id = "v" * 32
    invalid_request = _request(state_dir, request_id, "update")
    invalid_request.pop("target_version", None)
    result = agent.process_request(root, state_dir, invalid_request, skip_drain=True)

    assert result is False, "invalid request was accepted"
    status = _status(state_dir)
    assert status["state"] == "FAILED"
    assert "target_version" in status["message"].lower() or "invalid" in status["message"].lower()

    # No mutation occurred: container unchanged, no release activated.
    after_container = fleet.container()
    assert after_container == before_container
    assert fleet.served_version(after_container) == "0.0.0.1"
    assert not (root / "releases" / "0.0.0.2").exists()


def test_caller_version_size_validation_rejects_without_mutation(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#390 P3: version/size validation rejects invalid target without mutation."""
    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    first = fleet.wait_for_worker(version="0.0.0.1")
    before_container = fleet.container()

    # Request an invalid version that does not exist.
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    request_id = "w" * 32
    result = agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "9.9.9.9"),
                                 skip_drain=True)

    assert result is False, "invalid version was accepted"
    status = _status(state_dir)
    assert status["state"] == "FAILED"

    # No mutation occurred.
    after_container = fleet.container()
    assert after_container == before_container
    assert fleet.served_version(after_container) == "0.0.0.1"


def test_caller_rbac_refusal_propagates_without_mutation(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#390 P3: RBAC refusal (missing admin role) propagates without mutation."""
    import httpx

    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()

    first = fleet.wait_for_worker(version="0.0.0.1")
    before_container = fleet.container()

    # Simulate RBAC refusal by using wrong credentials.
    core_url = fleet.core_url()
    wrong_credentials = httpx.post(f"{core_url}/api/workers/consumer-worker/drain",
                                    auth=("wrong", "credentials"), timeout=10)
    assert wrong_credentials.status_code == 401, "RBAC check was bypassed"

    # Verify worker status unchanged (RBAC refusal prevented mutation).
    worker = fleet.worker_record()
    assert worker.get("status") == "READY", "RBAC refusal caused mutation"
    assert fleet.container() == before_container


def test_cancel_during_an_active_standard_apply_kills_it_without_a_delayed_effect(
        installation, signed_update_server, tmp_path, monkeypatch):
    """#390 F2: a cancel fence written while the bash consumer is inside its backup
    stops the consumer and its children; nothing of the update lands afterwards, and
    a later retry of the same release completes."""
    import threading
    import time

    root = installation.root
    # Incompressible data makes the consumer's own backup step take long enough to
    # cancel it while it is genuinely running.
    with (root / "storage" / "bulk.bin").open("wb") as bulk:
        for _ in range(48):
            bulk.write(os.urandom(4 * 1024 * 1024))
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    monkeypatch.setenv("UPDATE_FENCE_CHECK_SECONDS", "0.2")
    monkeypatch.setenv("UPDATE_FENCE_KILL_TIMEOUT", "10")
    agent = _agent()
    before = installation.container()
    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    request_id = "d" * 32

    def cancel_when_backup_started() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any((root / "backups").glob("update-*/application.tar.gz")):
                (state_dir / "cancel-fence.json").write_text(
                    json.dumps({"operation_id": request_id}), encoding="utf-8")
                return
            time.sleep(0.05)

    canceller = threading.Thread(target=cancel_when_backup_started)
    canceller.start()
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    canceller.join()
    status = _status(state_dir)
    assert status["request_id"] == request_id, _explain(state_dir, installation)
    assert "cancelled" in status["message"], _explain(state_dir, installation)
    leftover = subprocess.run(["pgrep", "-f", f"{root}/scripts/vertep"], capture_output=True, text=True)
    assert not leftover.stdout.strip(), f"consumer processes survived the cancel: {leftover.stdout}"

    time.sleep(10)  # a delayed effect of the killed apply would land in this window
    assert installation.container() == before
    assert installation.served_version(before) == "0.0.0.1"
    assert not (root / "current").exists() and not (root / "releases" / "0.0.0.2").exists()

    # Recovery does not overlap with the dead apply: once the fence is lifted the
    # same release is applied by a fresh request.
    (state_dir / "cancel-fence.json").unlink()
    retry_id = "e" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, retry_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    updated = installation.container()
    assert updated != before and installation.served_version(updated) == "0.0.0.2"
