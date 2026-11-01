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
    (payload / "manifest.json").write_text(
        json.dumps({"version": version, "images": {"worker": image}}), encoding="utf-8")
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
                                 skip_drain=True) is True, _status(state_dir)
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
                                 skip_drain=True) is True, _status(state_dir)
    assert _status(state_dir)["request_id"] == restart_id
    restarted = installation.container()
    assert restarted and restarted != updated
    assert installation.served_version(restarted) == "0.0.0.2"

    # rollback: the previous immutable release and its image are running again
    rollback_id = "3" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, rollback_id, "rollback"),
                                 skip_drain=True) is True, _status(state_dir)
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
                                 skip_drain=True) is False
    status = _status(state_dir)
    assert status["state"] == "FAILED" and status["request_id"] == request_id
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
    assert _status(state_dir)["request_id"] == request_id
    assert installation.container() == before, "a failed rescue still recreated the worker"
    assert installation.served_version(before) == "0.0.0.1"
    assert installation.consumer_records(before) == records == _records_digest(root)
    assert not (root / "releases" / "0.0.0.2").exists()
