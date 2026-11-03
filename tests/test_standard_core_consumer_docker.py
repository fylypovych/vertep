"""#390 F1/F2: the standard bash consumer updates a CORE node with its database.

A disposable CORE installation runs published Vertep CORE images with a real
PostgreSQL and Redis.  The update agent fetches a signed release and runs the shipped
``scripts/vertep``: it snapshots storage/config from the running core container,
dumps PostgreSQL, saves Redis, applies the release's SQL migrations, runs the
release's ``migrate`` backfills, recreates CORE and waits for its health.  A failing
migration must end in a correlated ROLLED_BACK result with the database restored
from the pre-update dump.

Boundary: on a real CORE host the consumer also installs systemd units.  A disposable
CI runner cannot do that without becoming root on the runner itself, so the consumer
runs on a host without ``systemctl`` (every other binary is the runner's own).
"""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.test_standard_host_consumer_docker import (
    CONSUMER_SCRIPTS, REQUIRED, ROOT, Installation, _agent, _archive, _explain, _pinned,
    _request, _run, _status, _usable)

RELEASE_IMAGES = {"0.0.0.1": "ghcr.io/fylypovych/vertep-core:0.0.2.94",
                  "0.0.0.2": "ghcr.io/fylypovych/vertep-core:0.0.2.96"}
POSTGRES_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7-alpine"
CORE_COMPOSE = """name: ${VERTEP_CONSUMER_PROJECT}
services:
  core:
    image: ${VERTEP_CORE_IMAGE}
    command: ["uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8080"]
    env_file: .env
    environment:
      DATABASE_URL: "host=postgres port=5432 dbname=vertep user=vertep password=${POSTGRES_PASSWORD}"
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
      CONFIG_ROOT: /data/config
      STORAGE_ROOT: /data/storage
      JOB_ROOT: /data/storage/jobs
      UPDATE_STATE_DIR: /tmp/core-update
      SYSTEM_STATE_BACKEND: file
      OLLAMA_URL: http://127.0.0.1:1
    volumes: ["./config:/data/config", "./storage:/data/storage"]
    ports: ["127.0.0.1:${VERTEP_FLEET_CORE_PORT}:8080"]
  postgres:
    image: ${VERTEP_POSTGRES_IMAGE}
    environment: {POSTGRES_DB: vertep, POSTGRES_USER: vertep, POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U vertep -d vertep"]
      interval: 2s
      retries: 60
  redis:
    image: ${VERTEP_REDIS_IMAGE}
    command: ["redis-server", "--requirepass", "${REDIS_PASSWORD}", "--appendonly", "yes"]
  migrate:
    image: ${VERTEP_CORE_IMAGE}
    command: ["python", "/app/scripts/migrate.py"]
    environment:
      DATABASE_URL: "host=postgres port=5432 dbname=vertep user=vertep password=${POSTGRES_PASSWORD}"
    volumes: ["./storage:/data/storage", "./config:/data/config"]
"""

pytestmark = pytest.mark.skipif(
    not _usable() and not REQUIRED, reason="standard host consumer needs bash and Docker")


@pytest.fixture(autouse=True)
def _require_docker():
    if REQUIRED and not _usable():
        pytest.fail("VERTEP_REQUIRE_STANDARD_HOST_CONSUMER=1 but bash/Docker is unavailable")


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _host_without_systemctl(directory: Path) -> str:
    """A PATH with every executable of this host except ``systemctl``."""
    directory.mkdir(parents=True, exist_ok=True)
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            source, link = os.path.join(folder, name), directory / name
            if name == "systemctl" or link.exists() or not os.access(source, os.X_OK):
                continue
            if os.path.isfile(source):
                link.symlink_to(source)
    return str(directory)


def _core_payload(base: Path, version: str, images: dict, extra_sql: dict | None = None) -> Path:
    payload = base / f"vertep-{version}"
    (payload / "scripts").mkdir(parents=True)
    (payload / "core").mkdir()
    (payload / "core" / "__init__.py").write_text("", encoding="utf-8")
    (payload / "config").mkdir()
    for name in CONSUMER_SCRIPTS:
        shutil.copyfile(ROOT / "scripts" / name, payload / "scripts" / name)
    shutil.copyfile(ROOT / "config" / "node_roles.json", payload / "config" / "node_roles.json")
    shutil.copytree(ROOT / "db", payload / "db", ignore=shutil.ignore_patterns("*.md", "__pycache__"))
    for name, sql in (extra_sql or {}).items():
        (payload / "db" / name).write_text(sql, encoding="utf-8")
    (payload / "VERSION").write_text(version + "\n", encoding="utf-8")
    (payload / "docker-compose.yml").write_text(CORE_COMPOSE, encoding="utf-8")
    (payload / "manifest.json").write_text(json.dumps(
        {"version": version, "images": images}), encoding="utf-8")
    return payload


class CoreInstallation(Installation):
    def psql(self, query: str) -> str:
        return self.compose("exec", "-T", "postgres", "psql", "-U", "vertep", "-d", "vertep",
                            "-Atc", query)

    def core_container(self) -> str:
        return _run("docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}",
                    "--filter", "label=com.docker.compose.service=core", "--filter", "status=running")

    def healthy(self, timeout: float = 240) -> int:
        import time

        import httpx

        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            try:
                last = httpx.get(f"http://127.0.0.1:{self.port}/api/health", timeout=5).status_code
                if last == 200:
                    return last
            except httpx.HTTPError as error:
                last = str(error)
            time.sleep(2)
        raise AssertionError(f"CORE never became healthy: {last}")


@pytest.fixture
def core_installation(tmp_path, monkeypatch):
    """A legacy flat CORE installation with a migrated PostgreSQL and Redis."""
    project = f"vertep-core-{uuid.uuid4().hex[:10]}"
    root = tmp_path / "opt-vertep"
    images = {"core": _pinned(RELEASE_IMAGES["0.0.0.1"]), "postgres": _pinned(POSTGRES_IMAGE),
              "redis": _pinned(REDIS_IMAGE)}
    images["comfyui"] = images["core"]
    shutil.copytree(_core_payload(tmp_path / "seed", "0.0.0.1", images), root)
    (root / "storage").mkdir()
    (root / "storage" / "records.json").write_text('{"approved": ["job-1"]}\n', encoding="utf-8")
    (root / "config" / "users.json").write_text("{}", encoding="utf-8")
    (root / "config" / "installation.json").write_text('{"completed_at": "2026-01-01"}', encoding="utf-8")
    port = _free_port()
    (root / ".env").write_text("\n".join([
        "ROLE=core", f"VERTEP_CONSUMER_PROJECT={project}", "VERTEP_VERSION=0.0.0.1",
        f"VERTEP_CORE_IMAGE={images['core']['reference']}@{images['core']['digest']}",
        f"VERTEP_POSTGRES_IMAGE={images['postgres']['reference']}@{images['postgres']['digest']}",
        f"VERTEP_REDIS_IMAGE={images['redis']['reference']}@{images['redis']['digest']}",
        "POSTGRES_PASSWORD=ci-consumer-postgres", "REDIS_PASSWORD=ci-consumer-redis",
        "ADMIN_USER=admin", "ADMIN_PASSWORD=ci-consumer-admin",
        f"VERTEP_CORE_URL=http://127.0.0.1:{port}", f"VERTEP_FLEET_CORE_PORT={port}", ""]),
        encoding="utf-8")
    installation = CoreInstallation(root, project)
    installation.port = port
    installation.compose("up", "-d", "--wait", "postgres", "redis")
    installation.compose("run", "--rm", "-T", "migrate")
    installation.compose("up", "-d", "core")
    installation.healthy()
    # CORE (root in its container) writes 0600 secrets into ./config; the host consumer
    # snapshots ./config as the unprivileged CI user, while production runs both as root.
    installation.compose("exec", "-T", "core", "chmod", "-R", "a+rX", "/data/config")
    installation.psql("CREATE TABLE ci_data(value text); INSERT INTO ci_data VALUES ('before-update');")
    monkeypatch.setenv("PATH", _host_without_systemctl(tmp_path / "host-bin"))
    yield installation
    subprocess.run(["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"],
                   capture_output=True)


def _environment(monkeypatch, root: Path, state_dir: Path) -> None:
    state_dir.mkdir()
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")


def _release_images() -> dict:
    images = {"core": _pinned(RELEASE_IMAGES["0.0.0.2"]), "postgres": _pinned(POSTGRES_IMAGE),
              "redis": _pinned(REDIS_IMAGE)}
    images["comfyui"] = images["core"]
    return images


def test_standard_consumer_updates_core_with_migrations_and_backfills(
        core_installation, signed_update_server, tmp_path, monkeypatch):
    root, state_dir = core_installation.root, tmp_path / "update-state"
    _environment(monkeypatch, root, state_dir)
    agent = _agent()
    before = core_installation.core_container()

    signed_update_server.publish("0.0.0.2", _archive(_core_payload(
        tmp_path / "release", "0.0.0.2", _release_images(),
        {"900_ci_release_marker.sql": "CREATE TABLE ci_release_marker(version text);\n"
                                      "INSERT INTO ci_release_marker VALUES ('0.0.0.2');\n"})))
    request_id = "f" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, core_installation)
    status = _status(state_dir)
    assert status["state"] == "SUCCEEDED" and status["request_id"] == request_id

    updated = core_installation.core_container()
    assert updated and updated != before, "CORE was not recreated"
    assert RELEASE_IMAGES["0.0.0.2"] in _run("docker", "inspect", "--format", "{{.Config.Image}}", updated)
    assert core_installation.healthy() == 200
    assert core_installation.psql("SELECT version FROM ci_release_marker") == "0.0.0.2"
    assert "900_ci_release_marker.sql" in core_installation.psql("SELECT version FROM schema_migrations")
    assert core_installation.psql("SELECT value FROM ci_data") == "before-update"
    backups = sorted((root / "backups").glob("update-*"))
    assert backups and (backups[-1] / "postgres.sql").stat().st_size > 0


def test_a_failing_migration_rolls_core_back_with_the_database_restored(
        core_installation, signed_update_server, tmp_path, monkeypatch):
    root, state_dir = core_installation.root, tmp_path / "update-state"
    _environment(monkeypatch, root, state_dir)
    agent = _agent()

    signed_update_server.publish("0.0.0.2", _archive(_core_payload(
        tmp_path / "release", "0.0.0.2", _release_images(),
        {"900_ci_release_marker.sql": "CREATE TABLE ci_release_marker(version text);\n",
         "901_ci_broken.sql": "THIS IS NOT SQL;\n"})))
    request_id = "0" * 32
    assert agent.process_request(root, state_dir, _request(state_dir, request_id, "update", "0.0.0.2"),
                                 skip_drain=True) is False
    status = _status(state_dir)
    assert status["state"] == "ROLLED_BACK" and status["request_id"] == request_id, \
        _explain(state_dir, core_installation)

    restored = core_installation.core_container()
    assert restored, "CORE is not running after the rollback"
    assert RELEASE_IMAGES["0.0.0.1"] in _run("docker", "inspect", "--format", "{{.Config.Image}}", restored)

    # Health check after rollback
    assert core_installation.healthy() == 200, "CORE not healthy after rollback"

    # Database rollback: half-applied migration must be rolled back
    assert core_installation.psql("SELECT to_regclass('public.ci_release_marker')") == "", \
        "the half-applied migration survived the rollback"

    # Data must be restored
    assert core_installation.psql("SELECT value FROM ci_data") == "before-update", \
        "data not restored after rollback"

    # Storage must be preserved
    assert (root / "storage" / "records.json").read_text(encoding="utf-8") == '{"approved": ["job-1"]}\n', \
        "storage not preserved after rollback"


def test_core_reads_back_its_jobs_and_approvals_across_update_rollback_and_recreate(
        core_installation, signed_update_server, tmp_path, monkeypatch):
    """#401 P1/P4: the application itself is the reader. A Job is created and approved
    through the CORE API; after a standard update with migrations, a failed update rolled
    back with the database restored, a retry with a fixed release, and a recreate of the CORE container, the running
    CORE reads the same Job back through its own API — identity, approval and the
    approval event — instead of a file or psql comparison made by the test."""
    import httpx

    root, state_dir = core_installation.root, tmp_path / "update-state"
    _environment(monkeypatch, root, state_dir)
    agent = _agent()
    api = httpx.Client(base_url=f"http://127.0.0.1:{core_installation.port}",
                       auth=("admin", "ci-consumer-admin"), timeout=30)

    created = api.post("/api/jobs", json={"topic": "Readback after update", "priority": 3})
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    approved = api.post(f"/api/jobs/{job_id}/approve")
    assert approved.status_code == 200, approved.text

    def read_back(stage: str) -> dict:
        core_installation.healthy()
        response = api.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, f"{stage}: {response.text}"
        job = response.json()
        events = api.get(f"/api/jobs/{job_id}/events")
        assert events.status_code == 200, f"{stage}: {events.text}"
        messages = json.dumps(events.json(), ensure_ascii=False)
        assert job["approved"] is True, f"{stage}: approval lost"
        assert "JOB APPROVED" in messages, f"{stage}: approval event lost"
        return {key: job[key] for key in ("job_id", "topic", "character_id", "created_at", "approved")}

    original = read_back("before")

    signed_update_server.publish("0.0.0.2", _archive(_core_payload(
        tmp_path / "release", "0.0.0.2", _release_images(),
        {"900_ci_release_marker.sql": "CREATE TABLE ci_release_marker(version text);\n"})))
    assert agent.process_request(root, state_dir, _request(state_dir, "d1" * 16, "update", "0.0.0.2"),
                                 skip_drain=True) is True, _explain(state_dir, core_installation)
    assert read_back("after update") == original

    signed_update_server.publish("0.0.0.3", _archive(_core_payload(
        tmp_path / "release-broken", "0.0.0.3", _release_images(),
        {"901_ci_broken.sql": "THIS IS NOT SQL;\n"})))
    assert agent.process_request(root, state_dir, _request(state_dir, "d2" * 16, "update", "0.0.0.3"),
                                 skip_drain=True) is False
    assert _status(state_dir)["state"] == "ROLLED_BACK", _explain(state_dir, core_installation)
    assert read_back("after rollback") == original

    signed_update_server.publish("0.0.0.4", _archive(_core_payload(
        tmp_path / "release-fixed", "0.0.0.4", _release_images(),
        {"902_ci_retry_marker.sql": "CREATE TABLE ci_retry_marker(version text);\n"})))
    assert agent.process_request(root, state_dir, _request(state_dir, "d3" * 16, "update", "0.0.0.4"),
                                 skip_drain=True) is True, _explain(state_dir, core_installation)
    assert read_back("after retry") == original

    core_installation.compose("up", "-d", "--force-recreate", "core")
    assert read_back("after recreate") == original
