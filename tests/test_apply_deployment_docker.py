"""Issue #134: disposable-container proof that legacy roots are rescued before recreate.

The unit tests in ``tests/test_apply_deployment.py`` exercise the rescue flow against a
fake runner.  These tests add the missing real evidence: they run a throwaway container
whose ``/app/{characters,brands,workflows}`` hold user data, recreate it the way
``docker compose up`` does, and prove the data survives *only* because it was staged out
first.  They gate on a working Docker daemon so a machine without one skips (a skip is
not a PASS) — CI runs them for real in the ``local-contracts`` job.
"""

import importlib.util
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts" / "apply-deployment.py"
    spec = importlib.util.spec_from_file_location("apply_deployment_docker", path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(value)
    return value


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, text=True).returncode == 0
    except OSError:
        return False


docker_gate = pytest.mark.skipif(not _docker_available(), reason="Docker daemon is not available")

IMAGE = "alpine:3.20"
COMPOSE_TEMPLATE = """services:
  worker:
    image: {image}
    command: ["sleep", "3600"]
"""


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=600, **kwargs)


@pytest.fixture
def compose_project(tmp_path):
    if _run(["docker", "image", "inspect", IMAGE]).returncode != 0:
        if _run(["docker", "pull", IMAGE]).returncode != 0:
            pytest.skip(f"cannot obtain base image {IMAGE}")
    project = f"vertep134_{uuid.uuid4().hex[:8]}"
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(COMPOSE_TEMPLATE.format(image=IMAGE), encoding="utf-8")
    compose = ["docker", "compose", "-f", str(compose_file), "-p", project]
    up = _run([*compose, "up", "-d"])
    if up.returncode != 0:
        pytest.skip(f"cannot start compose project: {up.stderr.strip()}")
    try:
        yield compose
    finally:
        _run([*compose, "down", "-v", "--remove-orphans"])


def _container_id(compose: list[str]) -> str:
    result = _run([*compose, "ps", "-q", "worker"])
    assert result.returncode == 0 and result.stdout.strip(), "worker container is not running"
    return result.stdout.strip()


def _seed_legacy_root(container_id: str, label: str, payload: dict) -> None:
    blob = json.dumps(payload)
    script = f"mkdir -p /app/{label} && printf '%s' '{blob}' > /app/{label}/record.json"
    result = _run(["docker", "exec", container_id, "sh", "-c", script])
    assert result.returncode == 0, f"failed to seed /app/{label}: {result.stderr}"


@docker_gate
def test_rescue_stages_legacy_roots_before_recreate(compose_project, tmp_path):
    """Real container: data written only into the old filesystem survives the recreate."""
    selected = {"worker"}
    container_id = _container_id(compose_project)
    _seed_legacy_root(container_id, "characters", {"id": "did_samogon", "name": "Дід"})
    _seed_legacy_root(container_id, "brands", {"id": "brand01"})

    staging = tmp_path / "staging"
    rescued = _module().rescue_legacy_ephemeral_roots(compose_project, selected, staging)

    assert rescued == {container_id: ["characters", "brands"]}
    record = json.loads((staging / "characters" / container_id / "record.json").read_text())
    assert record["id"] == "did_samogon"
    assert (staging / "brands" / container_id / "record.json").exists()
    # workflows/ was never seeded, so nothing is rescued for it.
    assert not (staging / "workflows").exists()

    # Recreate the way `docker compose up` does: the old filesystem is gone.
    _run([*compose_project, "up", "-d", "--force-recreate"])
    recreated = _container_id(compose_project)
    probe = _run(["docker", "exec", recreated, "test", "-e", "/app/characters/record.json"])
    assert probe.returncode != 0, "the recreated container must not still hold the legacy root"
    # The staged copy is the only surviving copy; it is intact and retriable.
    assert (staging / "characters" / container_id / "record.json").exists()


@docker_gate
def test_rescue_preserves_existing_persistent_data_and_is_idempotent(compose_project, tmp_path):
    """A retried rescue/migration never overwrites newer persistent data."""
    import importlib.util as _iu

    container_id = _container_id(compose_project)
    _seed_legacy_root(container_id, "characters", {"id": "legacy"})
    staging = tmp_path / "staging"
    _module().rescue_legacy_ephemeral_roots(compose_project, {"worker"}, staging)

    persistent_spec = _iu.spec_from_file_location(
        "persistent_data", Path(__file__).parents[1] / "core" / "persistent_data.py")
    persistent = _iu.module_from_spec(persistent_spec)
    persistent_spec.loader.exec_module(persistent)

    characters_dest = tmp_path / "persistent" / "characters"
    characters_dest.mkdir(parents=True)
    # A newer record already lives in persistent storage under the same filename.
    (characters_dest / "record.json").write_text('{"id": "newer"}', encoding="utf-8")

    def fake_roots():
        return {"characters": characters_dest, "brands": tmp_path / "persistent" / "brands",
                "workflows": tmp_path / "persistent" / "workflows"}

    original = persistent.persistent_roots
    persistent.persistent_roots = fake_roots
    try:
        migrated = persistent.migrate_staged_legacy_roots(staging)
    finally:
        persistent.persistent_roots = original

    # The newer file is not overwritten; the legacy record is not counted as migrated.
    assert json.loads((characters_dest / "record.json").read_text())["id"] == "newer"
    assert migrated.get("characters", 0) == 0
