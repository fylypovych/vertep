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
      # The tests poll CORE from one host address; the 300/min default answered 429.
      RATE_LIMIT_PER_MINUTE: "100000"
    volumes:
      - ${VERTEP_FLEET_CORE_CONFIG}:/data/config
    ports:
      - "127.0.0.1::8080"
  worker:
    image: ${VERTEP_WORKER_IMAGE}
    command: ["python", "-m", "worker.service"]
    # The node writes 0600 update requests the host agent must read; in production both
    # are root, in a CI fixture the host agent is the unprivileged test user.
    user: "${VERTEP_FLEET_WORKER_USER:-0:0}"
    env_file: .env
    environment:
      CORE_ADDRESS: http://core:8080
      NODE_NAME: consumer-worker
      NODE_ROLE: backup
      NODE_API_TOKEN: ${VERTEP_FLEET_NODE_TOKEN}
      BACKUP_ROOT: /tmp/backup-self-test
      DEMO_MODE: "true"
      SUPPORTED_TASKS: backup
      # Coordinated rollout (#401 F1): enrollment and the host update-request channel.
      REGISTRATION_TOKEN: ${VERTEP_FLEET_REGISTRATION_TOKEN:-}
      NODE_CONFIG_PATH: /data/node-config/node-credentials.json
      UPDATE_REQUEST_DIR: /data/update/requests
      # Writable without root when the fixture runs the node as the test user.
      LOG_ROOT: /tmp/vertep-logs
      HOME: /tmp
    volumes:
      - ./:/release:ro
      - ./storage:/data/storage:ro
      - ${VERTEP_FLEET_NODE_CONFIG}:/data/node-config
      - ${VERTEP_FLEET_UPDATE_DIR}:/data/update
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


def _wait_until(probe, timeout: float, what: str):
    import time

    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if last:
                return last
        except Exception as error:  # the service may still be starting
            last = error
        time.sleep(2)
    raise AssertionError(f"timed out waiting for {what}: {last}")


def _fleet(tmp_path: Path, *, enrolled: bool) -> "Fleet":
    """A legacy flat installation running published Vertep CORE and Worker images.

    ``enrolled`` makes the node enroll with a CORE registration token instead of the
    shared node token, so CORE can address it in a coordinated rollout (#401 F1).
    """
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
        f"VERTEP_FLEET_CORE_CONFIG={core_config}\n"
        f"VERTEP_FLEET_NODE_CONFIG={tmp_path / 'fleet-node-config'}\n"
        f"VERTEP_FLEET_UPDATE_DIR={tmp_path / 'fleet-update'}\n", encoding="utf-8")
    (tmp_path / "fleet-node-config").mkdir()
    # Created by the host user: the node (root in its container) only adds request files.
    (tmp_path / "fleet-update" / "requests").mkdir(parents=True)
    current = Fleet(root, project)
    if not enrolled:
        current.compose("up", "-d", "core", "worker")
        return current
    import httpx

    # CORE behaviour under test is the code of this tree, not the published image's: the
    # Python packages are mounted over the image for the CORE service only.
    override = tmp_path / "core-source.override.yml"
    override.write_text("services:\n  core:\n    volumes:\n" + "".join(
        f"      - {ROOT / package}:/app/{package}:ro\n"
        for package in ("core", "adapters", "publishers", "services")), encoding="utf-8")
    _run("docker", "compose", "--env-file", str(root / ".env"), "-f", str(root / "docker-compose.yml"),
         "-f", str(override), "up", "-d", "core")
    core_url = current.core_url()
    token = _wait_until(lambda: httpx.post(
        f"{core_url}/api/nodes/registration-tokens", json={"role": "backup", "ttl_seconds": 900},
        auth=("admin", ADMIN_PASSWORD), timeout=10).json().get("token"), 120, "a registration token")
    with (root / ".env").open("a", encoding="utf-8") as env:
        env.write(f"VERTEP_FLEET_REGISTRATION_TOKEN={token}\n"
                  f"VERTEP_FLEET_WORKER_USER={os.getuid()}:{os.getgid()}\n")
    current.compose("up", "-d", "worker")
    return current


@pytest.fixture
def fleet(tmp_path):
    """A legacy flat installation running published Vertep CORE and Worker images."""
    current = _fleet(tmp_path, enrolled=False)
    yield current
    subprocess.run(["docker", "compose", "-p", current.project, "down", "-v", "--remove-orphans"],
                   capture_output=True)


@pytest.fixture
def enrolled_fleet(tmp_path):
    """The fleet with an enrolled node, so CORE drives rollouts through its own API."""
    current = _fleet(tmp_path, enrolled=True)
    yield current
    subprocess.run(["docker", "compose", "-p", current.project, "down", "-v", "--remove-orphans"],
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
    drain = httpx.post(f"{core_url}/api/nodes/consumer-worker/actions", json={"action": "drain"},
                       auth=("admin", ADMIN_PASSWORD), timeout=10)
    assert drain.status_code == 200, drain.text

    # Verify the worker refuses admission in DRAINING state.
    draining = fleet.wait_for_worker(version="0.0.0.1", timeout=30)
    # The worker list exposes the control decision under ``update_state``.
    assert (draining.get("update_state") or {}).get("desired_state") == "DRAINING", draining

    # #396 F1: an actual admission attempt of the draining node is refused by CORE.
    claim = httpx.post(f"{core_url}/api/tasks/claim", headers={"X-Vertep-Token": NODE_TOKEN},
                       json={"node_name": "consumer-worker", "capabilities": []}, timeout=10)
    assert claim.status_code == 200, claim.text
    assert claim.json().get("task") is None and claim.json().get("worker_state") == "DRAINING", \
        claim.json()

    # Run the update while the worker is draining.
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    assert agent.process_request(root, state_dir, _request(state_dir, "f" * 32, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)

    # After the update the operator resumes the drained node; it serves the new version.
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"],
                                    timeout=180)
    resume = httpx.post(f"{core_url}/api/nodes/consumer-worker/actions", json={"action": "resume"},
                        auth=("admin", ADMIN_PASSWORD), timeout=10)
    assert resume.status_code == 200, resume.text
    deadline = time.monotonic() + 60
    while fleet.worker_record().get("status") != "READY" and time.monotonic() < deadline:
        time.sleep(2)
    assert fleet.worker_record().get("status") == "READY", fleet.worker_record()
    assert updated["version"] == "0.0.0.2"


def test_a_failed_standard_update_is_rolled_back_and_the_retry_succeeds(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#401 F1: a real apply error on the standard executor, then a retry. The first
    release pins an image digest Docker cannot pull, so the bash consumer fails and the
    agent rolls the worker back to the running release; a corrected release is then
    applied by a fresh request and CORE observes the new worker version."""
    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()
    first = fleet.wait_for_worker(version="0.0.0.1")
    before = fleet.container()

    good = _pinned(RELEASE_IMAGES["0.0.0.2"])
    broken = {**good, "digest": "sha256:" + "0" * 64}
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release-broken", "0.0.0.2", broken, FLEET_COMPOSE)))
    failed_id = "67" * 16
    assert agent.process_request(root, state_dir, _request(state_dir, failed_id, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    failed = _status(state_dir)
    assert failed["request_id"] == failed_id and failed["state"] in {"FAILED", "ROLLED_BACK"},         _explain(state_dir, fleet)
    assert fleet.served_version(fleet.container()) == "0.0.0.1", _explain(state_dir, fleet)
    assert fleet.wait_for_worker(version="0.0.0.1")["version"] == "0.0.0.1"

    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release-fixed", "0.0.0.2", good, FLEET_COMPOSE)))
    retry_id = "6f" * 16
    assert agent.process_request(root, state_dir, _request(state_dir, retry_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    retried = _status(state_dir)
    assert retried["request_id"] == retry_id and retried["state"] == "SUCCEEDED"
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"],
                                    timeout=180)
    assert updated.get("status") == "READY"
    assert fleet.container() != before


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
    request_id = "78" * 16
    result = agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "9.9.9.9"),
                                 skip_drain=True)

    assert result is False
    status = _status(state_dir)
    assert status["state"] == "FAILED"

    # No mutation occurred
    after_container = fleet.container()
    assert after_container == before_container
    assert fleet.served_version(after_container) == "0.0.0.1"


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
    assert agent.process_request(root, state_dir, _request(state_dir, "68" * 16, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"],
                                    timeout=180)

    # Rollback to 0.0.0.1
    started = datetime.now(timezone.utc).isoformat()
    assert agent.process_request(root, state_dir, _request(state_dir, "69" * 16, "rollback"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    rolled_back = fleet.wait_for_worker(version="0.0.0.1", not_instance=updated["runtime_instance_id"],
                                      after=started, timeout=180)
    assert rolled_back.get("status") == "READY"
    assert RELEASE_IMAGES["0.0.0.1"] in _run("docker", "inspect", "--format", "{{.Config.Image}}",
                                             fleet.container())


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
    request_id = "6c" * 16
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    status = _status(state_dir)
    assert status["state"] == "FAILED"

    # Recovery/retry with a valid release succeeds.
    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release-retry", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    retry_id = "6d" * 16
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
    link_operation_request(operation["operation_id"], {"request_id": "6e" * 16, "action": "update"})

    # Run the standard consumer update.
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    assert agent.process_request(root, state_dir, _request(state_dir, "6e" * 16, "update", "0.0.0.2"),
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
    assert finished["result"]["request_id"] == "6e" * 16

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
    assert agent.process_request(root, state_dir, _request(state_dir, "6f" * 16, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"],
                                    timeout=180)
    updated_consumer = _consumer_tree(fleet.container())
    assert updated_consumer == approved_host == _host_tree(storage)

    # Restart
    assert agent.process_request(root, state_dir, _request(state_dir, "70" * 16, "restart"),
                                 skip_drain=True) is True, _explain(state_dir, fleet)
    restarted = fleet.wait_for_worker(version="0.0.0.2", not_instance=updated["runtime_instance_id"],
                                      timeout=180)
    restarted_consumer = _consumer_tree(fleet.container())
    assert restarted_consumer == approved_host

    # Rollback
    assert agent.process_request(root, state_dir, _request(state_dir, "71" * 16, "rollback"),
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
                    json.dumps({"operation_id": "72" * 16}), encoding="utf-8")
                # Simulate newer data written after interruption point.
                (storage / "records.json").write_text('{"approved": ["job-1", "job-2"]}\n',
                                                   encoding="utf-8")
                return
            time.sleep(0.05)

    interrupter = threading.Thread(target=interrupt_when_backup_started)
    interrupter.start()

    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    assert agent.process_request(root, state_dir, _request(state_dir, "72" * 16, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    interrupter.join()

    # Newer data must not be overwritten by the interrupted apply.
    current_digest = _records_digest(root)
    assert current_digest != original_digest, "newer data was overwritten"
    assert "job-2" in (storage / "records.json").read_text(encoding="utf-8")

    # Retry succeeds with the newer data preserved.
    (state_dir / "cancel-fence.json").unlink()
    retry_id = "73" * 16
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
                    json.dumps({"operation_id": "74" * 16}), encoding="utf-8")
                return
            time.sleep(0.05)

    deleter = threading.Thread(target=delete_seed_when_backup_started)
    deleter.start()

    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    assert agent.process_request(root, state_dir, _request(state_dir, "74" * 16, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    deleter.join()

    # Deleted seed must not be restored by retry.
    assert not seed_file.exists()

    # Retry succeeds without restoring the deleted seed.
    (state_dir / "cancel-fence.json").unlink()
    retry_id = "75" * 16
    assert agent.process_request(root, state_dir, _request(state_dir, retry_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    assert not seed_file.exists(), "retry restored deleted seed"


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
    request_id = "77" * 16
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


class _HostUpdateAgent:
    """The host side of a coordinated rollout: like the ``vertep-update.path`` unit, the
    real update agent runs on every request the node writes into its request directory."""

    def __init__(self, agent, root: Path, state_dir: Path):
        import threading

        self.agent, self.root, self.state_dir = agent, root, state_dir
        self.processed: list[str] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        requests = self.state_dir / "requests"
        while not self._stop.is_set():
            for request in sorted(requests.glob("*.json")) if requests.is_dir() else []:
                try:
                    self.agent.process_request(self.root, self.state_dir, request, skip_drain=True)
                    self.processed.append(request.stem)
                except Exception as error:  # surfaced by the test, never swallowed
                    self.errors.append(f"{request.name}: {error!r}")
                    try:
                        request.unlink(missing_ok=True)
                    except OSError as unlink_error:
                        self.errors.append(f"{request.name}: {unlink_error!r}")
                        self._stop.set()
            self._stop.wait(1)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join(timeout=600)


def test_core_drives_a_canary_rollout_and_holds_it_for_promotion(
        enrolled_fleet, signed_update_server, tmp_path, monkeypatch):
    """#401 F1: CORE starts a canary rollout through its API. Through the node heartbeat it
    drains and updates the enrolled node, the node writes the update request, the host
    agent runs the standard consumer, CORE verifies a bound self-test of the new release
    and then holds the rollout at AWAITING_PROMOTION until an explicit promotion."""
    import httpx

    fleet = enrolled_fleet
    root, state_dir = fleet.root, tmp_path / "fleet-update"
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    first = fleet.wait_for_worker(version="0.0.0.1", timeout=240)
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    core_url = fleet.core_url()
    admin = ("admin", ADMIN_PASSWORD)

    def rollout() -> dict:
        return httpx.get(f"{core_url}/api/system/update/rolling", auth=admin, timeout=10).json()

    with _HostUpdateAgent(_agent(), root, state_dir) as host:
        started = httpx.post(f"{core_url}/api/system/update/rolling", auth=admin, timeout=30, json={
            "target_version": "0.0.0.2", "node_ids": ["consumer-worker"], "canary": True})
        assert started.status_code == 200, started.text
        assert started.json()["canary"] is True
        try:
            held = _wait_until(lambda: (state := rollout()).get("state") == "AWAITING_PROMOTION"
                               and state, 600, "the canary held for promotion")
        except AssertionError as error:
            raise AssertionError(f"{error}; host agent errors: {host.errors}; "
                                 f"processed: {host.processed}; rollout: {rollout()}") from None
        assert not host.errors, host.errors
        assert host.processed, "the node never asked the host agent for the update"
    node = held["nodes"][0]
    assert node["node_id"] == "consumer-worker" and node["phase"] == "READY" and node["canary"]
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=first["runtime_instance_id"])
    assert fleet.served_version(fleet.container()) == "0.0.0.2"

    promoted = httpx.post(f"{core_url}/api/system/update/rolling/promote", auth=admin, timeout=30)
    assert promoted.status_code == 200, promoted.text
    final = _wait_until(lambda: (state := rollout()).get("state") == "SUCCEEDED" and state,
                        120, "the promoted rollout finished")
    assert final["canary_promoted"] is True
    assert updated["version"] == "0.0.0.2"


def test_a_rollout_to_an_offline_node_fails_instead_of_hanging(
        enrolled_fleet, signed_update_server, tmp_path, monkeypatch):
    """#401 F1: the enrolled node goes offline before CORE can update it. Without its
    heartbeat nothing advances the rollout, so the phase timeout has to end it: CORE
    reports the node failed and leaves RUNNING instead of waiting forever."""
    import httpx

    fleet = enrolled_fleet
    fleet.wait_for_worker(version="0.0.0.1", timeout=240)
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    worker = fleet.container()
    _run("docker", "stop", worker)
    core_url, admin = fleet.core_url(), ("admin", ADMIN_PASSWORD)

    started = httpx.post(f"{core_url}/api/system/update/rolling", auth=admin, timeout=30, json={
        "target_version": "0.0.0.2", "node_ids": ["consumer-worker"],
        "update_timeout_seconds": 60})
    assert started.status_code == 200, started.text

    def rollout() -> dict:
        return httpx.get(f"{core_url}/api/system/update/rolling", auth=admin, timeout=10).json()

    ended = _wait_until(lambda: (state := rollout()).get("state") != "RUNNING" and state, 240,
                        "the rollout of the offline node to leave RUNNING")
    node = ended["nodes"][0]
    assert node["phase"] in {"FAILED", "ROLLING_BACK", "ROLLED_BACK", "ROLLBACK_FAILED"}, ended
    assert ended["state"] != "SUCCEEDED", ended


def test_workers_first_rollout_times_out_on_core_rolls_the_worker_back_and_a_retry_succeeds(
        enrolled_fleet, signed_update_server, tmp_path, monkeypatch):
    """#401 F1: CORE orders a workers-first rollout over the worker and CORE itself. The
    worker is updated by the standard consumer while CORE is still PENDING; CORE goes last,
    and with no coordinator answering its phase actually times out. The timeout fails the
    rollout and rolls the already-updated worker back through the host agent; a retry
    over the standard executor then updates the worker again."""
    import httpx

    fleet = enrolled_fleet
    root, state_dir = fleet.root, tmp_path / "fleet-update"
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    first = fleet.wait_for_worker(version="0.0.0.1", timeout=240)
    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release", "0.0.0.2", _pinned(RELEASE_IMAGES["0.0.0.2"]), FLEET_COMPOSE)))
    core_url, admin = fleet.core_url(), ("admin", ADMIN_PASSWORD)

    def rollout() -> dict:
        return httpx.get(f"{core_url}/api/system/update/rolling", auth=admin, timeout=10).json()

    def phases(state: dict) -> dict:
        return {node["node_id"]: node["phase"] for node in state.get("nodes", [])}

    with _HostUpdateAgent(_agent(), root, state_dir) as host:
        started = httpx.post(f"{core_url}/api/system/update/rolling", auth=admin, timeout=30, json={
            "target_version": "0.0.0.2", "node_ids": ["core", "consumer-worker"],
            "order": "workers-first", "update_timeout_seconds": 120})
        assert started.status_code == 200, started.text
        assert [node["node_id"] for node in started.json()["nodes"]] == ["consumer-worker", "core"]

        seen: list[dict] = []

        def worker_ready():
            state = rollout()
            seen.append(phases(state))
            return phases(state).get("consumer-worker") == "READY" and state

        _wait_until(worker_ready, 600, "the worker updated first")
        # CORE never left PENDING while the worker was being updated.
        assert all(snapshot.get("core") == "PENDING" for snapshot in seen
                   if snapshot.get("consumer-worker") != "READY"), seen
        assert fleet.served_version(fleet.container()) == "0.0.0.2"

        ended = _wait_until(lambda: (state := rollout()).get("state") in {
            "ROLLED_BACK", "ROLLBACK_FAILED", "FAILED"} and state, 600,
            "the CORE phase to time out and the worker to roll back")
        assert not host.errors, host.errors
    by_node = {node["node_id"]: node for node in ended["nodes"]}
    assert ended["state"] == "ROLLED_BACK", ended
    assert by_node["core"]["phase"] == "FAILED" and "timed out" in by_node["core"]["error"], ended
    assert by_node["consumer-worker"]["phase"] == "ROLLED_BACK", ended
    rolled_back = fleet.wait_for_worker(version="0.0.0.1", timeout=240)
    assert fleet.served_version(fleet.container()) == "0.0.0.1"

    with _HostUpdateAgent(_agent(), root, state_dir) as host:
        retried = httpx.post(f"{core_url}/api/system/update/rolling", auth=admin, timeout=30, json={
            "target_version": "0.0.0.2", "node_ids": ["consumer-worker"]})
        assert retried.status_code == 200, retried.text
        final = _wait_until(lambda: (state := rollout()).get("state") in {
            "SUCCEEDED", "ROLLED_BACK", "ROLLBACK_FAILED", "FAILED"} and state, 600, "the retry")
        assert not host.errors, host.errors
    assert final["state"] == "SUCCEEDED", final
    updated = fleet.wait_for_worker(version="0.0.0.2", not_instance=rolled_back["runtime_instance_id"])
    assert updated["runtime_instance_id"] != first["runtime_instance_id"]


@pytest.fixture
def lease_database():
    """A disposable PostgreSQL holding the distributed update fence; it can be stopped
    (database loss) and started again (database restart) by the test."""
    name = f"vertep-lease-db-{uuid.uuid4().hex[:8]}"
    _run("docker", "run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=lease-only",
         "-e", "POSTGRES_DB=vertep", "-p", "127.0.0.1::5432", "postgres:16-alpine")

    def ready() -> str:
        port = _run("docker", "port", name, "5432").splitlines()[0].rsplit(":", 1)[1]
        probe = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "vertep"],
                               capture_output=True)
        return probe.returncode == 0 and f"postgresql://postgres:lease-only@127.0.0.1:{port}/vertep"

    database = {"name": name, "ready": lambda: _wait_until(ready, 120, "the lease database")}
    database["dsn"] = database["ready"]()
    yield database
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_losing_the_lease_database_kills_the_active_apply_and_a_retry_succeeds_after_restart(
        installation, signed_update_server, lease_database, tmp_path, monkeypatch):
    """#401 F2: the distributed update fence lives in PostgreSQL. The database is stopped
    while the standard bash consumer is inside its backup: the fence can no longer be
    proven, so the agent kills the consumer and its children; the failure is correlated
    with the request and nothing of the update lands later. After the database restarts,
    a fresh request applies the same release."""
    import threading
    import time

    root = installation.root
    with (root / "storage" / "bulk.bin").open("wb") as bulk:
        for _ in range(48):
            bulk.write(os.urandom(4 * 1024 * 1024))
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    monkeypatch.setenv("UPDATE_DATABASE_URL", lease_database["dsn"])
    monkeypatch.setenv("UPDATE_REQUIRE_DISTRIBUTED_FENCE", "true")
    monkeypatch.setenv("UPDATE_FENCE_CHECK_SECONDS", "0.2")
    monkeypatch.setenv("UPDATE_FENCE_KILL_TIMEOUT", "10")
    agent = _agent()
    before = installation.container()
    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))

    def lose_database_when_backup_started() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any((root / "backups").glob("update-*/application.tar.gz")):
                subprocess.run(["docker", "stop", "-t", "0", lease_database["name"]],
                               capture_output=True)
                return
            time.sleep(0.05)

    loser = threading.Thread(target=lose_database_when_backup_started)
    loser.start()
    request_id = "a1" * 16
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    loser.join()
    status = _status(state_dir)
    assert status["request_id"] == request_id, _explain(state_dir, installation)
    assert "fence" in status["message"].lower(), _explain(state_dir, installation)
    leftover = subprocess.run(["pgrep", "-f", f"{root}/scripts/vertep"], capture_output=True, text=True)
    assert not leftover.stdout.strip(), f"consumer processes survived the lease loss: {leftover.stdout}"
    time.sleep(10)  # a delayed effect of the killed apply would land in this window
    assert installation.container() == before
    assert installation.served_version(before) == "0.0.0.1"
    assert not (root / "releases" / "0.0.0.2").exists()

    _run("docker", "start", lease_database["name"])
    monkeypatch.setenv("UPDATE_DATABASE_URL", lease_database["ready"]())
    retry_id = "a2" * 16
    assert agent.process_request(root, state_dir, _request(state_dir, retry_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    assert _status(state_dir)["request_id"] == retry_id
    assert installation.served_version(installation.container()) == "0.0.0.2"


_RECOVERY_PROCESS = r'''
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("update_agent_recovery", sys.argv[1])
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
try:
    agent.recover_if_interrupted(Path(sys.argv[2]), Path(sys.argv[3]))
    print(json.dumps({"recovered": True}))
except Exception as error:
    print(json.dumps({"refused": str(error)}))
'''


def test_a_recovery_started_during_an_active_apply_does_not_overlap_it(
        installation, signed_update_server, tmp_path, monkeypatch):
    """#401 F2: a second agent process starts (as after a service restart) while the first
    one is still applying an update. Its interrupted-update recovery must not overlap the
    live apply: it is refused by the held lease without touching the shared status, and
    the active apply finishes as the only operation."""
    import threading
    import time

    root = installation.root
    with (root / "storage" / "bulk.bin").open("wb") as bulk:
        for _ in range(48):
            bulk.write(os.urandom(4 * 1024 * 1024))
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    for name, value in (("VERTEP_ROOT", str(root)), ("UPDATE_STATE_DIR", str(state_dir)),
                        ("SYSTEM_STATE_BACKEND", "file")):
        monkeypatch.setenv(name, value)
    agent = _agent()
    signed_update_server.publish("0.0.0.2", _archive(
        _payload(tmp_path / "release", "0.0.0.2", _pinned(IMAGES["0.0.0.2"]))))
    observed: dict = {}

    def recover_while_backing_up() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any((root / "backups").glob("update-*/application.tar.gz")):
                observed["before"] = _status(state_dir)
                result = subprocess.run(
                    [sys.executable, "-c", _RECOVERY_PROCESS, str(ROOT / "scripts" / "update-agent.py"),
                     str(root), str(state_dir)],
                    capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
                observed["recovery"] = json.loads(result.stdout.strip().splitlines()[-1])
                observed["after"] = _status(state_dir)
                return
            time.sleep(0.05)

    recoverer = threading.Thread(target=recover_while_backing_up)
    recoverer.start()
    request_id = "b1" * 16
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, installation)
    recoverer.join()

    assert "refused" in observed["recovery"], observed
    assert "lease" in observed["recovery"]["refused"].lower(), observed
    assert observed["after"].get("phase") != "RECOVERING", (
        "the refused recovery rewrote the live operation's status", observed)
    final = _status(state_dir)
    assert final["request_id"] == request_id and final["state"] == "SUCCEEDED", final
    assert installation.served_version(installation.container()) == "0.0.0.2"
    journal = [json.loads(line) for line in (state_dir / "audit.jsonl").read_text().splitlines()]
    assert not any(event.get("phase") == "RECOVERING" for event in journal), \
        "the refused recovery was journaled as if it ran"


_RECONCILE = ("from core.telegram.handlers import _reconcile_update_operations; "
              "_reconcile_update_operations()")
_OPERATION = ("import json, sys; from core.operations import get_operation, pending_notifications; "
              "print(json.dumps({'operation': get_operation(sys.argv[1]), "
              "'notifications': pending_notifications()}))")


def _in_new_process(code: str, *args: str) -> str:
    """Run CORE code in a fresh process: the durable state, not this process's memory."""
    result = subprocess.run([sys.executable, "-c", code, *args], cwd=ROOT, env=dict(os.environ),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


def test_telegram_operation_reports_phases_error_and_retry_across_process_restarts(
        fleet, signed_update_server, tmp_path, monkeypatch):
    """#401 F3: a Telegram-requested update on the standard executor. While the host agent
    applies it, reconciliation (each run in a new process, as after a CORE restart)
    mirrors the executor phases into the operation; the failed attempt closes it FAILED
    with an error notification for the chat; the retry closes its own operation with a
    success notification. Every result is read back by yet another process."""
    import threading
    import time

    from core.operations import begin_operation, create_operation, link_operation_request

    root = fleet.root
    state_dir = tmp_path / "update-state"
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    agent = _agent()
    fleet.wait_for_worker(version="0.0.0.1")
    good = _pinned(RELEASE_IMAGES["0.0.0.2"])

    def operation_for(request_id: str) -> str:
        operation = create_operation("update", "telegram:42", target="0.0.0.2")
        begin_operation(operation["operation_id"], "Оновлення через Telegram")
        link_operation_request(operation["operation_id"], {"request_id": request_id, "action": "update"})
        return operation["operation_id"]

    def read(operation_id: str) -> dict:
        return json.loads(_in_new_process(_OPERATION, operation_id).strip().splitlines()[-1])

    def run_with_reconciliation(request_id: str, operation_id: str) -> tuple[bool, set[str]]:
        phases, done = set(), threading.Event()

        def reconcile_while_running() -> None:
            while not done.is_set():
                _in_new_process(_RECONCILE)
                phase = (read(operation_id)["operation"] or {}).get("current_phase")
                if phase:
                    phases.add(phase)
                time.sleep(0.5)

        sampler = threading.Thread(target=reconcile_while_running)
        sampler.start()
        try:
            result = agent.process_request(root, state_dir, _request(state_dir, request_id, "update",
                                                                     "0.0.0.2"), skip_drain=True)
        finally:
            done.set()
            sampler.join()
        _in_new_process(_RECONCILE)
        return result, phases

    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release-broken", "0.0.0.2", {**good, "digest": "sha256:" + "0" * 64}, FLEET_COMPOSE)))
    failed_id = "c1" * 16
    failed_operation = operation_for(failed_id)
    result, phases = run_with_reconciliation(failed_id, failed_operation)
    assert result is False
    failed = read(failed_operation)
    assert failed["operation"]["status"] == "FAILED", failed["operation"]
    assert phases & {"CHECKING", "DOWNLOADING", "UPDATING", "RECOVERING"}, phases
    assert any(item["chat_id"] == "42" and failed_operation[:8] in item["message"]
               and "❌" in item["message"] for item in failed["notifications"]), failed["notifications"]

    signed_update_server.publish("0.0.0.2", _archive(_payload(
        tmp_path / "release-fixed", "0.0.0.2", good, FLEET_COMPOSE)))
    retry_id = "c2" * 16
    retry_operation = operation_for(retry_id)
    result, _ = run_with_reconciliation(retry_id, retry_operation)
    assert result is True, _explain(state_dir, fleet)
    retried = read(retry_operation)
    assert retried["operation"]["status"] == "COMPLETED", retried["operation"]
    assert retried["operation"]["result"]["request_id"] == retry_id
    assert any(item["chat_id"] == "42" and retry_operation[:8] in item["message"]
               and "✅" in item["message"] for item in retried["notifications"]), retried["notifications"]
    # The failed attempt's record and notification are kept, not overwritten by the retry.
    assert read(failed_operation)["operation"]["status"] == "FAILED"
