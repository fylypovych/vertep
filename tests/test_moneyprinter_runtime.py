"""Issue #122 P2: the pinned, isolated MoneyPrinterTurbo runtime.

Covers the fail-closed guarantees of P2 without running the real container:

* ``adapters.providers.runtime_manifest`` — structural and pinned verification of
  the inventory the image build records, plus dependency drift detection.
* ``MoneyPrinterEngine`` — readiness that refuses a drifted runtime instead of
  degrading to the native engine.
* ``services.moneyprinter_service`` — the wrapper's snapshot gate, auth handling
  and stable failure codes.
* the packaging that makes the runtime opt-in: pinned Dockerfile inputs, the
  configuration lock that disables auto-upload, the compose profile, and the
  absence of MoneyPrinterTurbo from every node role.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import httpx
from starlette.testclient import TestClient

from adapters.providers.runtime_manifest import (
    INVENTORY_FORMAT,
    INVENTORY_REPOSITORY,
    RuntimeManifestError,
    build_inventory,
    dependency_drift,
    installed_dependencies,
    inventory_path,
    load_manifest,
    validate_inventory,
    verify_inventory,
)

PINNED_COMMIT = "2e1b30396e059e55939cc802c60faac2061e4d41"
BRIDGE_VERSION = "v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
MONEYPRINTER_DIR = REPO_ROOT / "docker" / "moneyprinter"


def _locked_config_values(text: str, section: str) -> dict:
    """Read ``key = value`` pairs of one TOML section without a TOML dependency.

    Mirrors ``scripts/qualify-release.py`` so both gates agree on what the lock says.
    """
    values: dict = {}
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            current = stripped.strip("[]")
            continue
        if current != section or "=" not in stripped or stripped.startswith("#"):
            continue
        key, _, raw = stripped.partition("=")
        raw = raw.strip()
        if raw in ("true", "false"):
            values[key.strip()] = raw == "true"
        elif raw.startswith('"') and raw.endswith('"'):
            values[key.strip()] = raw[1:-1]
        elif raw == "[]":
            values[key.strip()] = []
        else:
            values[key.strip()] = raw
    return values


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_inventory(root: Path, **overrides) -> Path:
    """Build a structurally valid inventory rooted at ``root``.

    Every declared file actually exists and is hashed the way the image build does
    it, so a test can isolate one violation at a time.
    """
    pinned = root / "pinned.txt"
    pinned.write_text("pinned upstream payload", encoding="utf-8")
    payload = build_inventory(
        repository=INVENTORY_REPOSITORY,
        commit=PINNED_COMMIT,
        version="2.0.3",
        bridge_version=BRIDGE_VERSION,
        bridge_schema_version=BRIDGE_VERSION,
        dependencies=["fastapi==0.115.0", "uvicorn==0.30.6"],
        file_digests={
            "pinned.txt": hashlib.sha256(pinned.read_bytes()).hexdigest(),
        },
    )
    if overrides:
        payload.update(overrides)
    inventory = root / "runtime-inventory.json"
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    return inventory


@pytest.fixture()
def inventory_root(tmp_path):
    return tmp_path


def load_manifest_inventory(root: Path, payload: dict):
    inventory = root / "runtime-inventory.json"
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    return load_manifest(inventory)


# ---------------------------------------------------------------------------
# Inventory verification is fail-closed
# ---------------------------------------------------------------------------


def test_valid_inventory_verifies(inventory_root):
    inventory = _write_inventory(inventory_root)

    manifest = verify_inventory(
        inventory, root=inventory_root, expected_commit=PINNED_COMMIT,
        expected_bridge_version=BRIDGE_VERSION,
        expected_bridge_schema_version=BRIDGE_VERSION,
    )

    assert manifest.upstream_commit == PINNED_COMMIT
    assert manifest.upstream_repository == INVENTORY_REPOSITORY
    assert manifest.upstream_reference == f"{INVENTORY_REPOSITORY}@{PINNED_COMMIT}"
    assert manifest.format == INVENTORY_FORMAT


def test_missing_inventory_is_rejected(tmp_path):
    with pytest.raises(RuntimeManifestError, match="missing"):
        verify_inventory(tmp_path / "absent.json")


def test_unreadable_inventory_is_rejected(tmp_path):
    broken = tmp_path / "runtime-inventory.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeManifestError, match="unreadable"):
        verify_inventory(broken)


def test_wrong_inventory_format_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root, format="something-else/v9")

    with pytest.raises(RuntimeManifestError, match="format"):
        verify_inventory(inventory, root=inventory_root)


def test_unpinned_upstream_repository_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"]["moneyprinter"]["repository"] = "attacker/MoneyPrinterTurbo"
    inventory.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeManifestError, match="harry0703/MoneyPrinterTurbo"):
        verify_inventory(inventory, root=inventory_root)


def test_short_commit_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"]["moneyprinter"]["commit"] = "2e1b3039"
    inventory.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeManifestError, match="40-hex"):
        verify_inventory(inventory, root=inventory_root)


def test_commit_drift_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root)

    with pytest.raises(RuntimeManifestError, match="commit drift"):
        verify_inventory(inventory, root=inventory_root, expected_commit="0" * 40)


def test_bridge_version_drift_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root)

    with pytest.raises(RuntimeManifestError, match="bridge version drift"):
        verify_inventory(inventory, root=inventory_root,
                         expected_bridge_version="v2")


def test_bridge_schema_drift_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root)

    with pytest.raises(RuntimeManifestError, match="bridge schema drift"):
        verify_inventory(inventory, root=inventory_root,
                         expected_bridge_schema_version="v2")


def test_empty_dependency_inventory_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root, dependencies=[])

    with pytest.raises(RuntimeManifestError, match="dependency inventory"):
        verify_inventory(inventory, root=inventory_root)


def test_unpinned_dependency_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root, dependencies=["fastapi"])

    with pytest.raises(RuntimeManifestError, match="not pinned"):
        verify_inventory(inventory, root=inventory_root)


def test_inventory_without_file_digests_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root, file_digests={})

    with pytest.raises(RuntimeManifestError, match="file digests"):
        verify_inventory(inventory, root=inventory_root)


def test_path_traversal_in_file_digests_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root, file_digests={"../etc/passwd": "0" * 64})

    with pytest.raises(RuntimeManifestError, match="file name is invalid"):
        verify_inventory(inventory, root=inventory_root)


def test_tampered_pinned_file_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root)
    (inventory_root / "pinned.txt").write_text("tampered payload", encoding="utf-8")

    with pytest.raises(RuntimeManifestError, match="was modified"):
        verify_inventory(inventory, root=inventory_root)


def test_removed_pinned_file_is_rejected(inventory_root):
    inventory = _write_inventory(inventory_root)
    (inventory_root / "pinned.txt").unlink()

    with pytest.raises(RuntimeManifestError, match="missing"):
        verify_inventory(inventory, root=inventory_root)


def test_digest_of_unchanged_inventory_is_stable(inventory_root):
    inventory = _write_inventory(inventory_root)

    first = load_manifest(inventory).compute_digest()
    second = load_manifest(inventory).compute_digest()

    assert first == second
    assert len(first) == 64


def test_validate_inventory_accepts_parsed_manifest(inventory_root):
    inventory = _write_inventory(inventory_root)

    manifest = validate_inventory(
        load_manifest(inventory), root=inventory_root, expected_commit=PINNED_COMMIT
    )

    assert manifest.verify(expected_commit=PINNED_COMMIT) is True
    assert manifest.verify(expected_commit="0" * 40) is False


def test_inventory_path_prefers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MONEYPRINTER_RUNTIME_INVENTORY", str(tmp_path / "custom.json"))

    assert inventory_path() == tmp_path / "custom.json"
    assert inventory_path(tmp_path / "explicit.json") == tmp_path / "explicit.json"


# ---------------------------------------------------------------------------
# Dependency inventory
# ---------------------------------------------------------------------------


def test_installed_dependencies_are_pinned_pairs():
    installed = installed_dependencies()

    assert installed, "the runtime must report its installed dependency inventory"
    assert all("==" in item for item in installed)
    assert list(installed) == sorted(installed)


def test_dependency_drift_reports_packages_outside_the_inventory(inventory_root):
    inventory = _write_inventory(inventory_root)
    manifest = load_manifest(inventory)

    drift = dependency_drift(manifest)

    installed = set(installed_dependencies())
    assert drift, "packages outside the pinned inventory must be reported"
    assert set(drift) <= installed
    assert not set(drift) & {"fastapi==0.115.0", "uvicorn==0.30.6"}


def test_dependency_drift_is_empty_for_the_real_environment(tmp_path):
    pinned = tmp_path / "pinned.txt"
    pinned.write_text("pinned upstream payload", encoding="utf-8")
    payload = build_inventory(
        repository=INVENTORY_REPOSITORY,
        commit=PINNED_COMMIT,
        version="2.0.3",
        bridge_version=BRIDGE_VERSION,
        bridge_schema_version=BRIDGE_VERSION,
        dependencies=list(installed_dependencies()),
        file_digests={},
    )
    manifest = load_manifest_inventory(tmp_path, payload)

    assert dependency_drift(manifest) == []


# ---------------------------------------------------------------------------
# Engine readiness
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """Minimal transport that replays queued responses and records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def _next(self, url, kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self._next(url, kwargs)

    def post(self, url, **kwargs):
        return self._next(url, kwargs)


def _inventory_payload(commit=PINNED_COMMIT, schema_version=BRIDGE_VERSION) -> dict:
    return {
        "format": INVENTORY_FORMAT,
        "components": {
            "moneyprinter": {
                "repository": INVENTORY_REPOSITORY,
                "commit": commit,
                "version": "2.0.3",
            }
        },
        "bridge_version": BRIDGE_VERSION,
        "bridge_schema_version": schema_version,
        "dependencies": ["fastapi==0.115.0"],
    }


def _engine(responses):
    from adapters.providers import MoneyPrinterEngine

    return MoneyPrinterEngine(url="http://moneyprinter:8098", token="k",
                              transport=_RecordingTransport(responses))


# The engine-side readiness contract (commit drift, schema drift, missing proof)
# lives in tests/test_video_engines.py. These tests cover the executor side of
# §9.12: the wrapper only reports ready when it proved auth and schema itself.


def test_wrapper_health_reports_ready_only_with_auth_and_schema_proof(
    monkeypatch, tmp_path
):
    from services import moneyprinter_service as wrapper

    client = _wrapper_client(monkeypatch, tmp_path)
    monkeypatch.setattr(wrapper, "check_upstream_authenticated", lambda *a, **k: None)
    monkeypatch.setattr(
        wrapper, "check_submit_schema",
        lambda: {name: "str" for name in wrapper.REQUIRED_SUBMIT_FIELDS},
    )

    response = client.get("/health")

    assert response.status_code == 200
    checks = response.json()["checks"]
    assert response.json()["status"] == "ready"
    assert checks["upstream_auth_enforced"] is True
    assert set(wrapper.REQUIRED_SUBMIT_FIELDS).issubset(checks["submit_schema"])
    assert checks["snapshot"]["image_digest"] == "sha256:" + "a" * 64


def test_wrapper_health_refuses_an_unauthenticated_upstream(monkeypatch, tmp_path):
    from services import moneyprinter_service as wrapper

    client = _wrapper_client(monkeypatch, tmp_path)

    def _answer_200_without_key(*args, **kwargs):
        raise wrapper.SelfTestFailure(
            wrapper.REASON_UPSTREAM_UNAUTHENTICATED,
            "pinned upstream API does not enforce x-api-key authentication",
        )

    monkeypatch.setattr(wrapper, "check_upstream_authenticated", _answer_200_without_key)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"]["reason"] == "upstream_unauthenticated"


def _raising_check_submit_schema(wrapper, code=None):
    def _check():
        raise wrapper.SelfTestFailure(
            code or wrapper.REASON_SCHEMA_UNSUPPORTED,
            "upstream TaskVideoRequest is missing required fields: video_aspect",
        )

    return _check


def test_wrapper_health_refuses_a_drifted_submit_schema(monkeypatch, tmp_path):
    from services import moneyprinter_service as wrapper

    client = _wrapper_client(monkeypatch, tmp_path)
    monkeypatch.setattr(wrapper, "check_upstream_authenticated", lambda *a, **k: None)
    monkeypatch.setattr(wrapper, "check_submit_schema", _raising_check_submit_schema(wrapper))

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"]["reason"] == "upstream_schema_unsupported"


def test_wrapper_health_keeps_independent_gate_results_separate(monkeypatch, tmp_path):
    """Auth may be proven while schema drifted; the report must not hide either fact."""
    from services import moneyprinter_service as wrapper

    client = _wrapper_client(monkeypatch, tmp_path)
    monkeypatch.setattr(wrapper, "check_upstream_authenticated", lambda *a, **k: None)
    monkeypatch.setattr(wrapper, "check_submit_schema", _raising_check_submit_schema(wrapper))

    response = client.get("/health")

    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "unavailable"
    assert body["checks"]["reason"] == "upstream_schema_unsupported"
    # The independently proven gate keeps its own truthful value; what makes the
    # runtime unusable is the refusal reason, not a rewritten earlier check.
    assert body["checks"]["upstream_auth_enforced"] is True
    assert "submit_schema" not in body["checks"]


def test_local_runtime_manifest_verifies_the_pinned_snapshot(inventory_root):
    from adapters.providers import MoneyPrinterEngine

    inventory = _write_inventory(inventory_root)
    engine = MoneyPrinterEngine(url="http://moneyprinter:8098", token="k")

    result = engine.verify_local_runtime_manifest(inventory)

    assert result["upstream_reference"] == f"{INVENTORY_REPOSITORY}@{PINNED_COMMIT}"
    assert result["bridge_schema_version"] == BRIDGE_VERSION
    assert result["dependency_count"] == 2


def test_local_runtime_manifest_refuses_drift(inventory_root):
    from adapters.providers import MoneyPrinterEngine

    inventory = _write_inventory(inventory_root)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"]["moneyprinter"]["commit"] = "0" * 40
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    engine = MoneyPrinterEngine(url="http://moneyprinter:8098", token="k")

    with pytest.raises(RuntimeManifestError, match="commit drift"):
        engine.verify_local_runtime_manifest(inventory)


# ---------------------------------------------------------------------------
# Wrapper snapshot gate
# ---------------------------------------------------------------------------


def _wrapper_client(monkeypatch, tmp_path, digest="sha256:" + "a" * 64):
    from services import moneyprinter_service as wrapper

    key_file = tmp_path / "api.key"
    key_file.write_text("runtime-key", encoding="utf-8")
    monkeypatch.setenv(wrapper.API_KEY_FILE_ENV, str(key_file))
    monkeypatch.setenv(wrapper.INVENTORY_PATH_ENV, str(_write_inventory(tmp_path)))
    monkeypatch.setenv(wrapper.INVENTORY_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(wrapper.IMAGE_DIGEST_ENV, digest)
    # Durable submit records are runtime state; keep them inside the test tmp dir.
    monkeypatch.setenv(wrapper.SUBMIT_RECORD_DIR_ENV, str(tmp_path / "submits"))
    return TestClient(wrapper.app)


def _raises(error):
    """A stand-in upstream that fails with a transport error."""
    def _raise(*args, **kwargs):
        raise error
    return _raise


def test_wrapper_runtime_reports_the_pinned_snapshot(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path)

    response = client.get("/runtime")

    assert response.status_code == 200
    body = response.json()
    assert body["upstream_reference"] == f"{INVENTORY_REPOSITORY}@{PINNED_COMMIT}"
    assert body["upstream_commit"] == PINNED_COMMIT
    assert body["image_digest"] == "sha256:" + "a" * 64
    assert body["bridge_schema_version"] == BRIDGE_VERSION


def test_wrapper_runtime_publishes_the_dependency_inventory(monkeypatch, tmp_path):
    """The full pinned dependency list is published, not just its size.

    This is the input Issue #122 P2 requires for the dependency inventory and the
    runtime SBOM; a count alone cannot be audited.
    """
    client = _wrapper_client(monkeypatch, tmp_path)

    body = client.get("/runtime").json()

    assert body["dependencies"] == ["fastapi==0.115.0", "uvicorn==0.30.6"]
    assert body["dependency_count"] == 2


def test_wrapper_publishes_an_sbom_of_the_verified_inventory(monkeypatch, tmp_path):
    """The runtime must be able to describe itself for the release manifest."""
    client = _wrapper_client(monkeypatch, tmp_path)

    response = client.get("/sbom")

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "sbom/v1"
    assert body["components"][0]["commit"] == PINNED_COMMIT
    assert body["dependencies"] == ["fastapi==0.115.0", "uvicorn==0.30.6"]
    assert len(body["digest"]) == 64


def test_wrapper_refuses_to_publish_an_sbom_without_a_verified_inventory(
    monkeypatch, tmp_path,
):
    client = _wrapper_client(monkeypatch, tmp_path)
    from services import moneyprinter_service as wrapper

    monkeypatch.setenv(
        wrapper.INVENTORY_PATH_ENV, str(tmp_path / "missing-inventory.json"),
    )

    response = client.get("/sbom")

    assert response.status_code == 503
    assert response.json()["reason"] == "runtime_inventory_unverified"


def test_runtime_inventory_produces_an_sbom(inventory_root):
    from adapters.providers.runtime_manifest import generate_sbom

    inventory = _write_inventory(inventory_root)
    sbom = generate_sbom(load_manifest(inventory))

    assert sbom["format"] == "sbom/v1"
    assert sbom["runtime_format"] == INVENTORY_FORMAT
    component = sbom["components"][0]
    assert component["name"] == "moneyprinter"
    assert component["repository"] == INVENTORY_REPOSITORY
    assert component["commit"] == PINNED_COMMIT
    assert sbom["dependencies"] == ["fastapi==0.115.0", "uvicorn==0.30.6"]
    assert len(sbom["digest"]) == 64


def test_pinned_dependency_lock_matches_the_published_inventory():
    """Every locked requirement must be a pinned pair the SBOM can reproduce."""
    lock = (MONEYPRINTER_DIR / "requirements.lock").read_text(encoding="utf-8")
    declared = {
        line.strip()
        for line in lock.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    assert declared, "the dependency inventory must not be empty"
    assert all(item.count("==") == 1 for item in declared)
    assert all("[" not in item and ";" not in item for item in declared)


def test_wrapper_refuses_a_missing_image_digest(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path, digest="")

    response = client.get("/runtime")

    assert response.status_code == 503
    assert response.json()["reason"] == "engine_snapshot_mismatch"


def test_wrapper_refuses_a_mutable_image_tag(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path, digest="moneyprinter:2.0.3")

    response = client.get("/runtime")

    assert response.status_code == 503
    assert response.json()["reason"] == "engine_snapshot_mismatch"


def test_wrapper_refuses_an_unverified_inventory(monkeypatch, tmp_path):
    from services import moneyprinter_service as wrapper

    client = _wrapper_client(monkeypatch, tmp_path)
    inventory = Path(wrapper.os.environ[wrapper.INVENTORY_PATH_ENV])
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"]["moneyprinter"]["commit"] = "0" * 40
    inventory.write_text(json.dumps(payload), encoding="utf-8")

    response = client.get("/runtime")

    assert response.status_code == 503
    assert response.json()["reason"] == "runtime_inventory_unverified"


def test_wrapper_health_reports_unavailable_when_upstream_is_down(monkeypatch, tmp_path):
    from services import moneyprinter_service as wrapper

    client = _wrapper_client(monkeypatch, tmp_path)
    monkeypatch.setattr(wrapper, "_upstream_url", lambda: "http://127.0.0.1:1")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"]["reason"] == "upstream_unreachable"


def test_wrapper_self_test_reports_schema_drift(monkeypatch, tmp_path):
    from services import moneyprinter_service as wrapper

    client = _wrapper_client(monkeypatch, tmp_path)
    monkeypatch.setattr(wrapper, "check_upstream_authenticated", lambda *a, **k: None)
    monkeypatch.setattr(wrapper, "check_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(wrapper, "check_media_pipeline", lambda: {"duration_seconds": 3.0})
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.models.schema",
        type("Schema", (), {"TaskVideoRequest": type("Request", (), {})})(),
    )

    response = client.get("/self-test")

    assert response.status_code == 503
    assert response.json()["reason"] == "upstream_schema_unsupported"


def test_wrapper_self_test_passes_when_every_gate_is_green(monkeypatch, tmp_path):
    from services import moneyprinter_service as wrapper

    client = _wrapper_client(monkeypatch, tmp_path)
    monkeypatch.setattr(wrapper, "check_upstream_authenticated", lambda *a, **k: None)
    monkeypatch.setattr(wrapper, "check_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(wrapper, "check_media_pipeline", lambda: {"duration_seconds": 3.0})
    monkeypatch.setattr(wrapper, "check_submit_route", lambda: _route_report())
    monkeypatch.setattr(
        wrapper, "check_submit_schema", lambda: {name: "str" for name in wrapper.REQUIRED_SUBMIT_FIELDS}
    )

    response = client.get("/self-test")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "passed"
    assert body["checks"]["upstream"]["authenticated"] is True
    # The whole route the factory uses is part of the eligibility gate, not only the
    # compose helper (Issue #122 P4).
    assert body["checks"]["submit_route"]["voice_staged"] is True


def test_wrapper_requires_a_non_empty_api_key(monkeypatch, tmp_path):
    from services import moneyprinter_service as wrapper

    client = _wrapper_client(monkeypatch, tmp_path)
    key_file = tmp_path / "empty.key"
    key_file.write_text("\n", encoding="utf-8")
    monkeypatch.setenv(wrapper.API_KEY_FILE_ENV, str(key_file))
    monkeypatch.setattr(wrapper, "_upstream_url", lambda: "http://127.0.0.1:1")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"]["reason"] == "upstream_unauthenticated"


# ---------------------------------------------------------------------------
# Packaging: pinned inputs, disabled upload, opt-in install
# ---------------------------------------------------------------------------


def test_dockerfile_pins_the_upstream_tarball():
    dockerfile = (MONEYPRINTER_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert PINNED_COMMIT in dockerfile
    assert "codeload.github.com/harry0703/MoneyPrinterTurbo/tar.gz" in dockerfile
    # The tarball digest is asserted during the build; a floating download is not
    # acceptable for a pinned runtime.
    assert "sha256" in dockerfile
    assert "MPT_TARBALL_SHA256" in dockerfile
    assert "cc2031d43b0d83d47efd631f1de7f3093df31fe26bb7374a28e7f05fa7d538a2" in dockerfile
    assert "sha256sum -c" in dockerfile


def test_dependency_lock_has_no_unpinned_requirement():
    lock = (MONEYPRINTER_DIR / "requirements.lock").read_text(encoding="utf-8")

    requirements = [
        line.strip()
        for line in lock.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert requirements, "the runtime must ship a dependency inventory"
    for requirement in requirements:
        assert "==" in requirement, requirement


def test_dependency_lock_declares_where_every_pin_comes_from():
    """The lock mixes upstream pins with pins upstream forgot to declare.

    Both groups are legitimate, but a pin must never appear without its
    provenance, or the inventory becomes unreviewable.
    """
    lock = (MONEYPRINTER_DIR / "requirements.lock").read_text(encoding="utf-8")

    assert "Declared by the pinned upstream commit" in lock
    assert "undeclared upstream" in lock
    for undeclared in ("toml==", "httpx==", "numpy==", "pillow==", "imageio-ffmpeg=="):
        assert undeclared in lock
    # The exclusions are a security decision and must stay documented in-tree.
    for excluded in ("faster-whisper", "streamlit", "audioop-lts", "twelvelabs"):
        assert excluded in lock


def test_dockerfile_stamps_the_vertep_version_and_upstream_pin():
    """An immutable digest is only traceable back to a release if the image says so."""
    dockerfile = (MONEYPRINTER_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG VERTEP_VERSION" in dockerfile
    assert 'org.opencontainers.image.version="${VERTEP_VERSION}"' in dockerfile
    assert 'io.vertep.moneyprinter.upstream-commit="${MPT_COMMIT}"' in dockerfile
    assert 'io.vertep.moneyprinter.bridge-schema-version="${BRIDGE_SCHEMA_VERSION}"' in dockerfile


def test_image_build_arguments_match_the_bridge_constants():
    """The inventory records build arguments; readiness verifies Python constants.

    If those two drift, a correctly built image is refused at start-up, so the
    defaults are pinned to the same source of truth the entrypoint imports.
    """
    from adapters.providers.base import BRIDGE_SCHEMA_VERSION, BRIDGE_VERSION
    from adapters.providers.runtime_manifest import PINNED_UPSTREAM_COMMIT

    dockerfile = (MONEYPRINTER_DIR / "Dockerfile").read_text(encoding="utf-8")
    defaults = dict(
        re.findall(r"(?m)^ARG (\w+)=(\S+)$", dockerfile)
    )

    assert defaults["MPT_COMMIT"] == PINNED_UPSTREAM_COMMIT
    assert defaults["BRIDGE_VERSION"] == BRIDGE_VERSION
    assert defaults["BRIDGE_SCHEMA_VERSION"] == BRIDGE_SCHEMA_VERSION


def test_entrypoint_verifies_both_bridge_versions_independently():
    """A single constant passed twice would let a schema drift pass unnoticed."""
    entrypoint = (MONEYPRINTER_DIR / "entrypoint.sh").read_text(encoding="utf-8")

    assert "expected_bridge_version=BRIDGE_VERSION" in entrypoint
    assert "expected_bridge_schema_version=BRIDGE_SCHEMA_VERSION" in entrypoint
    assert "expected_bridge_version=BRIDGE_SCHEMA_VERSION" not in entrypoint


def test_entrypoint_refuses_to_start_an_unauthenticated_upstream():
    """The pinned upstream only warns when its API key is empty."""
    entrypoint = (MONEYPRINTER_DIR / "entrypoint.sh").read_text(encoding="utf-8")

    assert "runtime API key is empty" in entrypoint
    assert 'config["app"]["api_key"] = api_key' in entrypoint
    assert "refusing to start" in entrypoint


def test_configuration_lock_keeps_auto_upload_disabled():
    text = (MONEYPRINTER_DIR / "config.lock.toml").read_text(encoding="utf-8")
    root_lock = _locked_config_values(text, "")
    app_lock = _locked_config_values(text, "app")

    assert root_lock["listen_host"] == "127.0.0.1"
    assert app_lock["upload_post_enabled"] is False
    assert app_lock["upload_post_auto_upload"] is False
    assert app_lock["upload_post_platforms"] == []
    assert app_lock["upload_post_api_key"] == ""
    assert app_lock["enable_redis"] is False
    assert app_lock["video_source"] == "local"
    assert app_lock["material_directory"] == "task"


def test_moneyprinter_is_not_part_of_any_node_role():
    roles = json.loads((REPO_ROOT / "config" / "node_roles.json").read_text(encoding="utf-8"))

    for role, definition in roles.items():
        if not isinstance(definition, dict) or not isinstance(definition.get("services"), list):
            continue
        assert "moneyprinter" not in definition["services"], role


def test_compose_keeps_the_runtime_opt_in():
    import yaml

    compose = yaml.safe_load(
        (REPO_ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    )
    service = compose["services"]["moneyprinter"]

    assert service["profiles"] == ["moneyprinter"]
    # The pinned upstream code must not inherit the platform secrets.
    assert "env_file" not in service
    assert "ports" not in service
    assert service["environment"]["MONEYPRINTER_API_KEY_FILE"] == (
        "/run/secrets/moneyprinter_api_key"
)
    assert "moneyprinter_api_key" in compose["secrets"]
    assert "moneyprinter-tasks" in compose["volumes"]

    # Optional means optional: without `--profile moneyprinter` the whole platform must
    # still come up on Native, so nothing that CORE needs may depend on this service.
    dependents = [
        name
        for name, other in compose["services"].items()
        if "moneyprinter" in (other.get("depends_on") or {})
    ]
    assert not dependents, f"moneyprinter is opt-in but {dependents} depend on it"
    for name, other in compose["services"].items():
        if name != "moneyprinter":
            assert "moneyprinter" not in (other.get("volumes") or []), (
                f"{name} mounts moneyprinter storage, so Native needs the profile"
            )


def test_release_builds_the_moneyprinter_image():
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "docker/moneyprinter/Dockerfile" in workflow


# ---------------------------------------------------------------------------
# The pin has exactly one home, and it stays importable inside the image
# ---------------------------------------------------------------------------


def test_pinned_reference_has_a_single_source():
    from adapters.providers.runtime_manifest import (
        PINNED_UPSTREAM_REFERENCE,
    )
    from adapters.providers.video_engines import MONEY_PRINTER_CONTRACT

    assert PINNED_UPSTREAM_REFERENCE == (
        f"{INVENTORY_REPOSITORY}@{PINNED_COMMIT}"
    )
    assert MONEY_PRINTER_CONTRACT.upstream_reference == PINNED_UPSTREAM_REFERENCE


def _module_level_imports(path: Path) -> set:
    """Return every module name imported at module level in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_wrapper_import_graph_stays_inside_the_image():
    """The container copies only ``adapters`` and ``services``.

    Anything the wrapper needs at runtime must therefore stay importable without
    the platform publishers that ``video_engines`` pulls in.
    """
    image_modules = {
        REPO_ROOT / "services" / "moneyprinter_service.py",
        REPO_ROOT / "adapters" / "providers" / "runtime_manifest.py",
        REPO_ROOT / "adapters" / "providers" / "base.py",
    }
    for module in image_modules:
        imports = _module_level_imports(module)
        assert not any(
            name.startswith("publishers") or name.endswith("video_engines")
            for name in imports
        ), f"{module.name} imports outside the image: {sorted(imports)}"


def test_dockerfile_copies_what_the_wrapper_imports():
    dockerfile = (MONEYPRINTER_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY adapters /opt/vertep/adapters" in dockerfile
    assert "COPY services /opt/vertep/services" in dockerfile
    # The pin must come from the shared module, not from a second literal.
    assert "PINNED_UPSTREAM_REPOSITORY" in dockerfile
    assert 'repository="harry0703/MoneyPrinterTurbo"' not in dockerfile


def test_entrypoint_renders_config_into_the_pinned_app_root():
    """Upstream loads ``<app-root>/config.toml``; the entrypoint must match that.

    Rendering anywhere else would leave the API running on upstream defaults,
    which is exactly the silent drift Issue #122 forbids.
    """
    entrypoint = (MONEYPRINTER_DIR / "entrypoint.sh").read_text(encoding="utf-8")

    assert 'APP_DIR="/opt/moneyprinter"' in entrypoint
    assert 'CONFIG_FILE="${APP_DIR}/config.toml"' in entrypoint
    assert "expected_commit=PINNED_UPSTREAM_COMMIT" in entrypoint


def test_compose_does_not_make_the_app_root_read_only():
    """The pinned upstream saves config.toml atomically inside its own root.

    A read-only root filesystem would make every start fail, so the compose file
    must not declare one; the compensating controls are asserted separately.
    """
    import yaml

    compose = yaml.safe_load(
        (REPO_ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    )
    service = compose["services"]["moneyprinter"]

    assert not service.get("read_only")
    assert service["tmpfs"]
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert "ports" not in service


# ---------------------------------------------------------------------------
# The release gate must actually bite
#
# A gate that passes is only meaningful if it fails when the property it protects
# is broken. These tests copy the repository, break exactly one P2 property and
# assert that the matching qualification check reports it.
# ---------------------------------------------------------------------------

_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "web-v2", "logs", ".codeatlas", ".local", ".opencode", ".kilo",
    "__pycache__", "*.pyc", ".pytest_cache", ".test-update", "node_modules",
)


def _repo_copy(destination: Path) -> Path:
    shutil.copytree(REPO_ROOT, destination, ignore=_COPY_IGNORE)
    return destination


def _qualify_checks(root: Path) -> dict:
    script = root / "scripts" / "qualify-release.py"
    spec = importlib.util.spec_from_file_location("qualify_release_copy", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {check["name"]: check for check in module.qualify(root)["checks"]}


def test_release_gate_passes_on_the_current_repository(tmp_path):
    checks = _qualify_checks(_repo_copy(tmp_path / "repo"))

    assert checks["moneyprinter_opt_in"]["passed"] is True
    assert checks["moneyprinter_auto_upload_disabled"]["passed"] is True
    assert checks["moneyprinter_upstream_isolated"]["passed"] is True
    assert checks["optional_engine_stays_out_of_roles"]["passed"] is True


def test_release_gate_catches_a_re_enabled_auto_upload(tmp_path):
    root = _repo_copy(tmp_path / "repo")
    lock = root / "docker/moneyprinter/config.lock.toml"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            "upload_post_enabled = false", "upload_post_enabled = true"
        ),
        encoding="utf-8",
    )

    checks = _qualify_checks(root)

    assert checks["moneyprinter_auto_upload_disabled"]["passed"] is False


def test_release_gate_catches_an_upstream_listener_rebind(tmp_path):
    root = _repo_copy(tmp_path / "repo")
    lock = root / "docker/moneyprinter/config.lock.toml"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            'listen_host = "127.0.0.1"', 'listen_host = "0.0.0.0"'
        ),
        encoding="utf-8",
    )

    checks = _qualify_checks(root)

    assert checks["moneyprinter_upstream_isolated"]["passed"] is False


def test_release_gate_catches_a_non_opt_in_service(tmp_path):
    root = _repo_copy(tmp_path / "repo")
    compose = root / "deploy/docker-compose.yml"
    compose.write_text(
        compose.read_text(encoding="utf-8").replace(
            '    profiles: ["moneyprinter"]\n', ""
        ),
        encoding="utf-8",
    )

    checks = _qualify_checks(root)

    assert checks["moneyprinter_opt_in"]["passed"] is False


def test_release_gate_catches_a_published_runtime_port(tmp_path):
    root = _repo_copy(tmp_path / "repo")
    compose = root / "deploy/docker-compose.yml"
    compose.write_text(
        compose.read_text(encoding="utf-8").replace(
            '    expose: ["8098"]', '    ports: ["8098:8098"]'
        ),
        encoding="utf-8",
    )

    checks = _qualify_checks(root)

    assert checks["moneyprinter_opt_in"]["passed"] is False


def test_release_gate_catches_moneyprinter_inside_a_node_role(tmp_path):
    root = _repo_copy(tmp_path / "repo")
    roles_path = root / "config/node_roles.json"
    roles = json.loads(roles_path.read_text(encoding="utf-8"))
    roles["core"]["services"].append("moneyprinter")
    roles_path.write_text(json.dumps(roles, indent=2), encoding="utf-8")

    checks = _qualify_checks(root)

    assert checks["optional_engine_stays_out_of_roles"]["passed"] is False


def test_release_gate_catches_a_missing_pinned_build_input(tmp_path):
    root = _repo_copy(tmp_path / "repo")
    (root / "docker/moneyprinter/config.lock.toml").unlink()

    checks = _qualify_checks(root)

    assert checks["moneyprinter_auto_upload_disabled"]["passed"] is False
    assert checks["required_release_files"]["passed"] is False


# ---------------------------------------------------------------------------
# Disposable runtime verification (Issue #122 P2)
# ---------------------------------------------------------------------------


def _dockerfile_arg(name: str) -> str:
    for line in (MONEYPRINTER_DIR / "Dockerfile").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"ARG {name}="):
            return stripped.split("=", 1)[1].strip().strip('\"')
    raise AssertionError(f"Dockerfile does not pin {name}")


def _load_runtime_check():
    path = REPO_ROOT / "scripts/moneyprinter-runtime-check.py"
    spec = importlib.util.spec_from_file_location("moneyprinter_runtime_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_check_builds_the_pinned_image_without_redeclaring_pins():
    """The verification must build exactly the image the repository defines."""
    check = _load_runtime_check()

    for argument in ("MPT_COMMIT", "MPT_TARBALL_SHA256", "MPT_VERSION"):
        assert check._build_arg(argument) == _dockerfile_arg(argument)
    assert PINNED_COMMIT in check._build_arg("MPT_COMMIT")


def test_runtime_check_refuses_a_runtime_with_commit_drift():
    check = _load_runtime_check()

    with pytest.raises(check.CheckFailure, match="upstream commit"):
        check.verify_runtime(
            {
                "upstream_commit": "0" * 40,
                "bridge_schema_version": BRIDGE_VERSION,
                "image_digest": "sha256:" + "a" * 64,
                "inventory_digest": "b" * 64,
                "dependencies": ["fastapi==0.115.0"],
                "dependency_count": 1,
            },
            "sha256:" + "a" * 64,
        )


def test_runtime_check_refuses_a_runtime_that_reports_another_image_digest():
    check = _load_runtime_check()

    with pytest.raises(check.CheckFailure, match="image digest"):
        check.verify_runtime(
            {
                "upstream_commit": PINNED_COMMIT,
                "bridge_schema_version": BRIDGE_VERSION,
                "image_digest": "sha256:" + "b" * 64,
                "inventory_digest": "c" * 64,
                "dependencies": ["fastapi==0.115.0"],
                "dependency_count": 1,
            },
            "sha256:" + "a" * 64,
        )


def test_runtime_check_refuses_an_unpinned_dependency_inventory():
    check = _load_runtime_check()

    with pytest.raises(check.CheckFailure, match="unpinned runtime dependency"):
        check.verify_runtime(
            {
                "upstream_commit": PINNED_COMMIT,
                "bridge_schema_version": BRIDGE_VERSION,
                "image_digest": "sha256:" + "a" * 64,
                "inventory_digest": "c" * 64,
                "dependencies": ["fastapi"],
                "dependency_count": 1,
            },
            "sha256:" + "a" * 64,
        )


def _route_report(**overrides) -> dict:
    """A completed submit → status → download proof through the real route (P4).

    The shape mirrors :func:`check_submit_route`: ``state`` is the pinned runtime's own
    finished marker, and the colours prove the download shows the submitted scene.
    """
    report = {
        "submit_key": "self-test-1-1",
        "task_id": "j-self-test",
        "state": 1,
        "bytes": 4096,
        "duration_seconds": 2.0,
        "width": 1080,
        "height": 1920,
        "mean_colour": [200.0, 10.0, 10.0],
        "expected_colour": [200.0, 10.0, 10.0],
        "voice_staged": True,
        "output": "final/video-20260101-000000.mp4",
    }
    report.update(overrides)
    return report


def _ready_health(**checks) -> dict:
    """A ``/health`` report that satisfies every §9.12 readiness gate.

    The submit schema is built from the bridge contract so this report cannot claim to
    be ready while proving less than the engine requires.
    """
    from adapters.providers.base import REQUIRED_SUBMIT_FIELDS

    payload = {
        "snapshot": {"image_digest": "sha256:" + "a" * 64},
        "upstream_auth_enforced": True,
        "submit_schema": sorted(REQUIRED_SUBMIT_FIELDS),
        "media_pipeline": {"bytes": 4096, "duration_seconds": 3.0},
    }
    payload.update(checks)
    return {"status": "ready", "checks": payload}


def test_wrapper_proves_inherited_optional_submit_fields_without_failing():
    """``video_script``/``video_materials``/``custom_audio_file`` are inherited fields.

    They live on the pinned ``VideoParams``, so a proof that only looked at the
    request's own fields would report a healthy runtime as drifted. Their annotations are
    also ``Optional[...]``, which has no guaranteed ``__name__``, so describing them must
    not turn the proof into an error.
    """
    import typing

    from adapters.providers.base import REQUIRED_SUBMIT_FIELDS
    from services import moneyprinter_service as wrapper

    class _Field:
        def __init__(self, annotation):
            self.annotation = annotation

    class _MaterialInfo:
        pass

    class _VideoAspect:
        pass

    fields = {
        "video_subject": _Field(str),
        "video_script": _Field(str),
        "video_materials": _Field(typing.Optional[typing.List[_MaterialInfo]]),
        "custom_audio_file": _Field(typing.Optional[str]),
        "video_aspect": _Field(typing.Optional[_VideoAspect]),
        "video_source": _Field(typing.Optional[str]),
        "subtitle_enabled": _Field(bool),
        "video_clip_duration": _Field(int),
    }
    request = type("TaskVideoRequest", (), {"model_fields": fields})
    module = type("schema", (), {"TaskVideoRequest": request})
    monkey = pytest.MonkeyPatch()
    monkey.setitem(__import__("sys").modules, "app.models.schema", module)
    try:
        proved = wrapper.check_submit_schema()
    finally:
        monkey.undo()

    assert set(REQUIRED_SUBMIT_FIELDS).issubset(fields)
    assert set(proved) == set(REQUIRED_SUBMIT_FIELDS)
    assert all(isinstance(value, str) and value for value in proved.values())


def test_runtime_check_reads_the_contract_when_run_as_a_script():
    """CI runs ``python scripts/moneyprinter-runtime-check.py``, not pytest.

    That invocation puts ``scripts/`` on the path instead of the repository root, so
    importing the bridge contract failed with ``No module named 'adapters'`` — a failure
    no test running under pytest can see, because pytest already provides the root. This
    runs the module the way CI does and asserts the gate reaches its verdict.
    """
    import subprocess

    program = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('runtime_check', r'{REPO_ROOT / 'scripts' / 'moneyprinter-runtime-check.py'}');"
        "module = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(module);"
        "health = {'status': 'ready', 'checks': {'snapshot': {'image_digest': 'sha256:' + 'a' * 64},"
        " 'upstream_auth_enforced': True, 'submit_schema': ['video_subject'],"
        " 'media_pipeline': {'bytes': 4096, 'duration_seconds': 3.0}}};"
        "\ntry:\n module.verify_gate(health)\nexcept module.CheckFailure as refusal:\n"
        "    print('REFUSED', refusal)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT / "scripts",
    )

    assert completed.returncode == 0, completed.stderr
    assert "REFUSED" in completed.stdout
    # The refusal must come from the contract, not from a missing import.
    assert "submit schema is missing" in completed.stdout


def test_runtime_check_reads_only_keys_the_wrapper_health_actually_publishes(
    monkeypatch, tmp_path,
):
    """The gate must not require a field the health report does not carry.

    ``/health`` is a cheap readiness report; the produced media file is proven by the
    self-test gate. Requiring ``media_pipeline`` here failed a fully healthy runtime for
    a field its report never publishes, which no unit test of either side alone could see.
    """
    from services import moneyprinter_service as wrapper

    check = _load_runtime_check()
    monkeypatch.setattr(wrapper, "check_upstream_authenticated", lambda *a, **k: None)
    monkeypatch.setattr(wrapper, "check_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        wrapper, "check_media_pipeline",
        lambda: {"duration_seconds": 9.0, "bytes": 4096,
                 "scenes": [{"label": "scene-1", "seconds": 9, "at_seconds": 4.5,
                             "mean_colour": [1.0, 1.0, 1.0],
                             "expected_colour": [1.0, 1.0, 1.0]}],
                 "scene_order": ["scene-1"], "expected_scene_order": ["scene-1"],
                 "aspect": "9:16", "fit_mode": "contain"},
    )
    monkeypatch.setattr(wrapper, "check_submit_route", lambda: _route_report())
    monkeypatch.setattr(
        wrapper,
        "check_submit_schema",
        lambda: {name: "str" for name in wrapper.REQUIRED_SUBMIT_FIELDS},
    )
    client = _wrapper_client(monkeypatch, tmp_path)

    published = set(client.get("/health").json()["checks"])

    assert set(check.HEALTH_GATE_KEYS).issubset(published), (
        "the readiness gate reads fields the wrapper's /health does not publish"
    )
    assert "media_pipeline" not in check.HEALTH_GATE_KEYS
    # The media proof has to stay enforced somewhere: the self-test report.
    self_test = client.get("/self-test").json()
    assert self_test["checks"]["media_pipeline"]["bytes"] == 4096
    assert self_test["checks"]["submit_route"]["task_id"] == "j-self-test"
    check.verify_self_test(self_test)


def test_runtime_check_requires_a_proven_submit_route(monkeypatch, tmp_path):
    """A runtime that cannot render through its own submit route is not eligible.

    The compose helper alone proved nothing about the route the factory uses, so the
    gate also requires a completed submit → status → download with the approved voice
    staged (Issue #122 P4).
    """
    from services import moneyprinter_service as wrapper

    check = _load_runtime_check()
    monkeypatch.setattr(wrapper, "check_upstream_authenticated", lambda *a, **k: None)
    monkeypatch.setattr(wrapper, "check_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(wrapper, "check_media_pipeline", lambda: {
        "duration_seconds": 9.0, "bytes": 4096,
        "scenes": [{"label": "scene-1", "seconds": 9, "at_seconds": 4.5,
                    "mean_colour": [1.0, 1.0, 1.0], "expected_colour": [1.0, 1.0, 1.0]}],
        "scene_order": ["scene-1"], "expected_scene_order": ["scene-1"],
        "aspect": "9:16", "fit_mode": "contain",
    })
    monkeypatch.setattr(wrapper, "check_submit_route", lambda: _route_report())
    monkeypatch.setattr(
        wrapper, "check_submit_schema",
        lambda: {name: "str" for name in wrapper.REQUIRED_SUBMIT_FIELDS},
    )
    client = _wrapper_client(monkeypatch, tmp_path)
    passing = client.get("/self-test").json()
    check.verify_self_test(passing)

    for broken, expected in (
        ({"voice_staged": False}, "approved voice"),
        ({"task_id": None}, "no upstream task"),
        ({"state": 0}, "did not finish"),
        ({"bytes": 0}, "no media bytes"),
        ({"duration_seconds": 0}, "no media duration"),
        ({"submit_key": ""}, "no durable submit key"),
        ({"width": 1920, "height": 1080}, "9:16 aspect"),
        ({"mean_colour": [10.0, 200.0, 200.0]}, "did not render the submitted scene"),
    ):
        monkeypatch.setattr(wrapper, "check_submit_route",
                            lambda report={**broken}: _route_report(**report))
        failing = client.get("/self-test").json()
        with pytest.raises(check.CheckFailure, match=expected):
            check.verify_self_test(failing)

    # A runtime that reports no submit route at all is refused as well.
    monkeypatch.setattr(wrapper, "check_submit_route", lambda: {})
    with pytest.raises(check.CheckFailure, match="submit route"):
        check.verify_self_test(client.get("/self-test").json())


def test_runtime_check_requires_every_readiness_gate():
    check = _load_runtime_check()

    check.verify_gate(_ready_health())

    with pytest.raises(check.CheckFailure, match="without a verified snapshot"):
        check.verify_gate({"status": "ready", "checks": {"upstream_auth_enforced": True}})
    with pytest.raises(check.CheckFailure, match="without a proven upstream authentication"):
        check.verify_gate({
            "status": "ready",
            "checks": {
                "snapshot": {"image_digest": "sha256:" + "a" * 64},
                "upstream_auth_enforced": False,
            },
        })
    with pytest.raises(check.CheckFailure, match="submit schema is missing"):
        check.verify_gate({
            "status": "ready",
            "checks": {
                "snapshot": {"image_digest": "sha256:" + "a" * 64},
                "upstream_auth_enforced": True,
                "submit_schema": [],
            },
        })
    # The media proof belongs to the self-test report, so a zero byte count must be
    # refused there — not by the readiness gate, whose report never carries it.
    check.verify_gate(_ready_health())
    with pytest.raises(check.CheckFailure, match="without a produced media file"):
        check.verify_self_test({
            "status": "passed",
            "checks": {"media_pipeline": {"bytes": 0, "duration_seconds": 3.0}},
        })
    with pytest.raises(check.CheckFailure, match="undecodable media file"):
        check.verify_self_test({
            "status": "passed",
            "checks": {"media_pipeline": {"bytes": 4096, "duration_seconds": 0}},
        })


def _media_report(**overrides) -> dict:
    """A self-test media report that proves scene order, durations and aspect."""
    report = {
        "duration_seconds": 9.0,
        "bytes": 4096,
        "width": 1080,
        "height": 1920,
        "scenes": [
            {"label": "scene-1", "seconds": 2, "at_seconds": 1.0,
             "mean_colour": [200.0, 10.0, 10.0], "expected_colour": [200.0, 10.0, 10.0]},
            {"label": "scene-2", "seconds": 3, "at_seconds": 3.5,
             "mean_colour": [10.0, 200.0, 10.0], "expected_colour": [10.0, 200.0, 10.0]},
            {"label": "scene-3", "seconds": 4, "at_seconds": 7.0,
             "mean_colour": [10.0, 10.0, 200.0], "expected_colour": [10.0, 10.0, 200.0]},
        ],
        "scene_order": ["scene-1", "scene-2", "scene-3"],
        "expected_scene_order": ["scene-1", "scene-2", "scene-3"],
        "expected_duration_seconds": 9,
        "aspect": "9:16",
        "fit_mode": "contain",
        "concat_mode": "sequential",
    }
    report.update(overrides)
    # Every self-test the gate accepts also carries the real submit-route proof, so a
    # fixture that is only about the compose path cannot silently drift out of contract.
    return {"media_pipeline": report, "submit_route": _route_report()}


def test_runtime_check_requires_a_proven_scene_timeline():
    """§9.3 rows 2–4/8 are the runtime's own duty, so the gate has to prove them.

    A self-test that merely says "media file produced" cannot tell a runtime that keeps
    scene order and the approved scene durations from one that reorders or re-times the
    scenes, so the reported timeline is required and every scene is compared with the
    clip it had to come from.
    """
    check = _load_runtime_check()

    check.verify_self_test({"status": "passed", "checks": _media_report()})

    with pytest.raises(check.CheckFailure, match="without a proven scene timeline"):
        check.verify_self_test({
            "status": "passed",
            "checks": {"media_pipeline": {"bytes": 4096, "duration_seconds": 9.0}},
        })
    reordered = _media_report()
    reordered["media_pipeline"]["scenes"] = list(
        reversed(reordered["media_pipeline"]["scenes"]))
    reordered["media_pipeline"]["scene_order"] = ["scene-3", "scene-2", "scene-1"]
    with pytest.raises(check.CheckFailure, match="out of order"):
        check.verify_self_test({"status": "passed", "checks": reordered})

    lying = _media_report()
    lying["media_pipeline"]["scene_order"] = ["scene-1", "scene-2", "scene-3"]
    lying["media_pipeline"]["scenes"][1]["mean_colour"] = [10.0, 10.0, 200.0]
    with pytest.raises(check.CheckFailure, match="does not match its approved clip"):
        check.verify_self_test({"status": "passed", "checks": lying})

    outside = _media_report()
    outside["media_pipeline"]["scenes"][2]["at_seconds"] = 30.0
    with pytest.raises(check.CheckFailure, match="outside the rendered timeline"):
        check.verify_self_test({"status": "passed", "checks": outside})

    without_aspect = _media_report()
    without_aspect["media_pipeline"].pop("fit_mode")
    with pytest.raises(check.CheckFailure, match="aspect/fit mode"):
        check.verify_self_test({"status": "passed", "checks": without_aspect})


def test_wrapper_media_proof_refuses_a_wrong_scene_timeline(monkeypatch):
    """The in-image proof fails closed on order, duration and aspect drift."""
    from services import moneyprinter_service as wrapper

    correct = [
        {"label": "scene-1", "seconds": 2, "at_seconds": 1.0,
         "mean_colour": [200.0, 10.0, 10.0], "expected_colour": [200.0, 10.0, 10.0]},
        {"label": "scene-2", "seconds": 3, "at_seconds": 3.5,
         "mean_colour": [10.0, 200.0, 10.0], "expected_colour": [10.0, 200.0, 10.0]},
        {"label": "scene-3", "seconds": 4, "at_seconds": 7.0,
         "mean_colour": [10.0, 10.0, 200.0], "expected_colour": [10.0, 10.0, 200.0]},
    ]
    reordered = [
        dict(correct[0]),
        dict(correct[1], mean_colour=[10.0, 10.0, 200.0]),
        dict(correct[2]),
    ]

    class _Aspect:
        portrait = None

        @staticmethod
        def to_resolution():
            return 1080, 1920

    _Aspect.portrait = _Aspect
    wrapper._verify_probe_timeline(correct, 9, 1080, 1920, _Aspect)

    with pytest.raises(wrapper.SelfTestFailure, match="scene order or duration is wrong"):
        wrapper._verify_probe_timeline(reordered, 9, 1080, 1920, _Aspect)
    with pytest.raises(wrapper.SelfTestFailure, match="ignored the job aspect"):
        wrapper._verify_probe_timeline(correct, 9, 1920, 1080, _Aspect)
    with pytest.raises(wrapper.SelfTestFailure, match="runs past the approved duration"):
        wrapper._verify_probe_timeline(
            [dict(entry, at_seconds=entry["at_seconds"] + 20) for entry in correct],
            9, 1080, 1920, _Aspect)
    with pytest.raises(wrapper.SelfTestFailure, match="of 3 scenes"):
        wrapper._verify_probe_timeline(correct[:1], 9, 1080, 1920, _Aspect)


def test_runtime_check_refuses_a_runtime_that_dropped_a_script_or_voice_field():
    """The §9.3 inputs must be proved, not just the fields that start a task.

    A runtime that stopped accepting the script, the staged clips or the approved voice
    can still accept a bare subject and would then regenerate content the factory owns,
    so the gate takes its expected fields from the bridge contract instead of keeping a
    copy of its own.
    """
    from adapters.providers.base import REQUIRED_SUBMIT_FIELDS

    check = _load_runtime_check()

    for dropped in ("video_script", "video_materials", "custom_audio_file"):
        assert dropped in REQUIRED_SUBMIT_FIELDS
        proved = [name for name in REQUIRED_SUBMIT_FIELDS if name != dropped]

        with pytest.raises(check.CheckFailure) as refusal:
            check.verify_gate(_ready_health(submit_schema=proved))

        assert dropped in str(refusal.value)

    check.verify_gate(_ready_health())


def test_runtime_check_identifies_a_locally_built_image_by_its_content_digest():
    """A locally built image has no ``RepoDigests``.

    Reading ``RepoDigests`` made every clean verification run fail closed before the
    runtime was ever asked anything, so the content identity of the image that was
    just built is what identifies the deployed runtime.
    """
    check = _load_runtime_check()
    image_id = "sha256:" + "a" * 64
    answers = {
        ("image", "inspect", "--format", "{{.Id}}", check.IMAGE_NAME): image_id,
        ("image", "inspect", "--format", "{{json .RepoDigests}}", check.IMAGE_NAME): "[]",
    }
    check.docker = lambda *args, **kwargs: answers[tuple(args)]

    assert check.image_digest(check.IMAGE_NAME) == image_id
    assert check.registry_digest(check.IMAGE_NAME) == ""

    answers[("image", "inspect", "--format", "{{.Id}}", check.IMAGE_NAME)] = "not-a-digest"
    with pytest.raises(check.CheckFailure, match="immutable content digest"):
        check.image_digest(check.IMAGE_NAME)


def test_runtime_check_reads_an_empty_repo_digest_list_without_failing():
    """`{{index .RepoDigests 0}}` makes the daemon fail on an empty slice.

    A locally built image has no registry digest, and that is normal — reading it as
    an index must not end the verification before the runtime is ever asked anything.
    """
    check = _load_runtime_check()
    template = ("image", "inspect", "--format", "{{json .RepoDigests}}", check.IMAGE_NAME)
    check.docker = lambda *args, **kwargs: "[]" if tuple(args) == template else ""

    assert check.registry_digest(check.IMAGE_NAME) == ""

    check.docker = lambda *args, **kwargs: (
        '["vertep/moneyprinter-runtime-check@sha256:' + "b" * 64 + '"]'
        if tuple(args) == template else "")
    assert check.registry_digest(check.IMAGE_NAME).endswith("@sha256:" + "b" * 64)


def _module_roots(path: Path) -> set[str]:
    """Top-level module names imported by one Python file."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class _ImportCollector(ast.NodeVisitor):
    """Collect the imports that actually run when a module is imported.

    Imports inside a function or a class body only run when that code runs, so the
    wrapper does not need the package at import time: treating them as requirements
    would force unrelated Vertep packages into the isolated image.
    """

    def __init__(self, package: str) -> None:
        self.package = package
        self.modules: set[str] = set()

    def visit_FunctionDef(self, node):  # noqa: N802 - ast visitor protocol
        return None

    def visit_AsyncFunctionDef(self, node):  # noqa: N802 - ast visitor protocol
        return None

    def visit_ClassDef(self, node):  # noqa: N802 - ast visitor protocol
        for statement in node.body:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                self.visit(statement)
        return None

    def visit_Import(self, node):  # noqa: N802 - ast visitor protocol
        self.modules.update(alias.name for alias in node.names)

    def visit_ImportFrom(self, node):  # noqa: N802 - ast visitor protocol
        if node.level:
            base = self.package.split(".")
            trimmed = base[:len(base) - (node.level - 1)] if node.level > 1 else base
            prefix = ".".join(trimmed)
            if node.module:
                self.modules.add(f"{prefix}.{node.module}" if prefix else node.module)
            else:
                self.modules.update(f"{prefix}.{alias.name}" if prefix else alias.name
                                     for alias in node.names)
        elif node.module:
            self.modules.add(node.module)


def _imported_modules(path: Path) -> set[str]:
    """Absolute dotted modules that importing one Python file requires.

    Relative imports are resolved against the importing module's package, because
    importing a submodule also executes every ``__init__.py`` on the way — and those
    package inits are exactly where a cross-package import like
    ``adapters.providers -> publishers.transport`` hides.
    """
    package = ".".join(path.with_suffix("").parts[:-1])
    collector = _ImportCollector(package)
    collector.visit(ast.parse(path.read_text(encoding="utf-8")))
    return collector.modules


def _module_file(module: str) -> Path | None:
    parts = module.split(".")
    candidate = Path(*parts).with_suffix(".py")
    if candidate.is_file():
        return candidate
    package = Path(*parts) / "__init__.py"
    return package if package.is_file() else None


def _package_inits(module: str) -> list[Path]:
    """``__init__.py`` of every package that importing ``module`` executes."""
    parts = module.split(".")[:-1]
    inits: list[Path] = []
    for depth in range(1, len(parts) + 1):
        init = Path(*parts[:depth]) / "__init__.py"
        if init.is_file():
            inits.append(init)
    return inits


def _image_packages(dockerfile: str) -> set[str]:
    """Top-level packages the image copies into ``/opt/vertep``."""
    return set(re.findall(r"^COPY\s+(\w+)\s+/opt/vertep/\1\s*$", dockerfile, re.MULTILINE))


def test_the_image_copies_every_package_the_wrapper_imports():
    """A package the wrapper imports but the image does not copy cannot be imported.

    ``adapters`` reaches ``publishers.transport``, so the wrapper image needs the
    publisher stack too; the CI build proved it by failing on ``No module named
    'publishers'`` while recording the runtime inventory.
    """
    dockerfile = Path("docker/moneyprinter/Dockerfile").read_text(encoding="utf-8")
    copied = _image_packages(dockerfile)
    assert copied, "the image must copy the wrapper packages"

    # Follow the real import graph of the modules the entrypoint loads: importing the
    # wrapper must not need a package the image does not ship.
    queue = [Path("services/moneyprinter_service.py"),
             Path("adapters/providers/runtime_manifest.py"),
             Path("adapters/providers/base.py")]
    seen: set[Path] = set()
    required: set[str] = set()
    while queue:
        current = queue.pop()
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        for module in _imported_modules(current):
            root = module.split(".")[0]
            if root in sys.stdlib_module_names:
                continue
            if not (Path(root) / "__init__.py").is_file():
                continue  # third-party dependency, pinned in requirements.lock
            required.add(root)
            resolved = _module_file(module)
            if resolved is not None:
                queue.append(resolved)
            queue.extend(_package_inits(module))

    assert required <= copied, (
        f"the wrapper imports packages the image does not copy: "
        f"{sorted(required - copied)}"
    )


def test_runtime_check_mounts_the_api_key_directory_the_entrypoint_reads():
    """The entrypoint reads ``<mount>/api_key``.

    Mounting the key *file* onto the mount path itself would turn that path into a
    file, so the runtime would refuse to start with an unreadable key.
    """
    check = _load_runtime_check()
    recorded: dict = {}

    def fake_docker(*args, **kwargs):
        recorded["args"] = args
        return ""

    check.docker = fake_docker
    check.start_container("c", "sha256:" + "a" * 64, Path("/host/keys"))

    mounts = [str(value) for value in recorded["args"] if str(value).startswith("type=bind")]
    assert len(mounts) == 1
    assert mounts[0].endswith("target=/run/vertep-runtime-check,readonly")
    assert "api_key" not in mounts[0], \
        "the key file itself must not be mounted onto the directory the entrypoint reads"
    assert "MONEYPRINTER_API_KEY_FILE=/run/vertep-runtime-check/api_key" in recorded["args"]
    # The API is proven only through the wrapper port; no published upstream port.
    assert "127.0.0.1::8098" in recorded["args"]
    assert not [value for value in recorded["args"]
                if isinstance(value, str) and value.count(":") == 1 and value.endswith(":8080")]


def test_dockerfile_bounded_transfer_uses_real_curl_options():
    """`--timeout` is not a curl option and made the image build fail on CI."""
    dockerfile = Path("docker/moneyprinter/Dockerfile").read_text(encoding="utf-8")

    assert "--max-time" in dockerfile
    assert "curl -fsSL --retry 3 --timeout" not in dockerfile


def test_runtime_check_leaves_the_mounted_key_readable_for_the_image_user():
    """The runtime runs as its own non-root uid, so a 0700 host mount is unreadable.

    A bind mount keeps the host permissions, so an ephemeral key written by the gate is
    invisible to that uid and the entrypoint fails closed before it ever listens — which
    surfaces as a container that never publishes a port.
    """
    import stat

    check = _load_runtime_check()

    with tempfile.TemporaryDirectory() as directory:
        key_directory = Path(directory)
        key_file, api_key = check.write_api_key(key_directory)

        assert key_file.name == "api_key"
        assert key_file.read_text(encoding="utf-8").strip() == api_key
        assert api_key
        assert stat.S_IMODE(key_file.stat().st_mode) & stat.S_IROTH
        # The directory must be traversable, not just the file readable.
        assert stat.S_IMODE(key_directory.stat().st_mode) & stat.S_IXOTH


def test_runtime_check_reports_the_container_log_before_it_is_discarded(monkeypatch, capsys):
    """A runtime that never answers must leave its own log behind as evidence.

    The container is removed in ``finally``, so without this the gate can only report
    that a port was never published and never says why.
    """
    check = _load_runtime_check()
    monkeypatch.setattr(check, "docker_available", lambda: True)
    monkeypatch.setattr(check, "build_image", lambda version: None)
    monkeypatch.setattr(check, "image_digest", lambda name: "sha256:" + "a" * 64)
    monkeypatch.setattr(check, "registry_digest", lambda name: "")
    monkeypatch.setattr(check, "start_container", lambda *a, **k: None)
    monkeypatch.setattr(
        check,
        "mapped_port",
        lambda name: (_ for _ in ()).throw(check.CheckFailure("no public port '8098'")),
    )
    reported: dict = {}

    def fake_report(name, lines=60):
        reported["name"] = name

    monkeypatch.setattr(check, "report_container_log", fake_report)
    monkeypatch.setattr(
        check.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "", "stderr": ""})()
    )
    monkeypatch.setattr(
        check.argparse.ArgumentParser,
        "parse_args",
        lambda self, *a, **k: type("A", (), {"keep": True, "timeout": 1, "evidence": ""})(),
    )

    assert check.main() == 1
    assert reported["name"].startswith("moneyprinter-runtime-check-")
    assert "no public port" in capsys.readouterr().err


def test_runtime_check_refuses_a_re_enabled_auto_upload_config():
    check = _load_runtime_check()

    # The locked config, including the account visibility labels the lock pins.
    check.verify_config_auto_upload.__globals__["docker"] = lambda *args, **kwargs: (
        'upload_post_enabled = false\n'
        'upload_post_api_key = ""\n'
        'upload_post_username = ""\n'
        'upload_post_platforms = []\n'
        'upload_post_auto_upload = false\n'
        'upload_post_youtube_privacy_status = "private"\n'
        'upload_post_youtube_made_for_kids = false\n'
        'upload_post_max_pending_tasks = 0\n'
    )
    check.verify_config_auto_upload("container")

    check.verify_config_auto_upload.__globals__["docker"] = lambda *args, **kwargs: (
        "upload_post_enabled = false\n"
        "upload_post_auto_upload = true\n"
    )
    with pytest.raises(check.CheckFailure, match="auto-upload is enabled"):
        check.verify_config_auto_upload("container")


def test_runtime_check_refuses_every_way_the_runtime_could_publish():
    """Publishing needs an enable switch, a credential or a configured platform.

    A visibility label such as ``upload_post_youtube_privacy_status`` cannot publish
    anything, so refusing it would be refusing a value the lock deliberately pins — but
    every real switch must still be caught, including one that reappears with a name the
    check has never seen.
    """
    check = _load_runtime_check()
    locked = (
        'upload_post_enabled = false\n'
        'upload_post_api_key = ""\n'
        'upload_post_platforms = []\n'
        'upload_post_auto_upload = false\n'
        'upload_post_youtube_privacy_status = "private"\n'
    )

    for reenabled in (
        "upload_post_enabled = true",
        "upload_post_auto_upload = true",
        'upload_post_api_key = "token"',
        'upload_post_platforms = ["youtube"]',
        'upload_post_youtube_cookies = "session=1"',
        'upload_post_tiktok_token = "token"',
    ):
        check.verify_config_auto_upload.__globals__["docker"] = (
            lambda *args, **kwargs: locked + reenabled + "\n"
        )
        with pytest.raises(check.CheckFailure, match="auto-upload is enabled"):
            check.verify_config_auto_upload("container")

    # A config that dropped the switches entirely is refused as well, not accepted
    # because nothing is enabled.
    check.verify_config_auto_upload.__globals__["docker"] = (
        lambda *args, **kwargs: "upload_post_youtube_privacy_status = \"private\"\n"
    )
    with pytest.raises(check.CheckFailure, match="auto-upload"):
        check.verify_config_auto_upload("container")


def test_runtime_check_requires_a_self_test_that_produced_media():
    check = _load_runtime_check()

    check.verify_self_test({
        "status": "passed",
        "checks": _media_report(bytes=2048, duration_seconds=9.0),
    })
    with pytest.raises(check.CheckFailure, match="self-test did not pass"):
        check.verify_self_test({"status": "failed", "reason": "upstream_unreachable"})
    with pytest.raises(check.CheckFailure, match="without a produced media file"):
        check.verify_self_test({
            "status": "passed", "checks": {"media_pipeline": {"bytes": 0, "duration_seconds": 3.0}},
        })


def test_ci_runs_the_disposable_runtime_verification():
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "scripts/moneyprinter-runtime-check.py" in workflow
    assert "--evidence moneyprinter-runtime-evidence" in workflow


def test_wrapper_forwards_a_scene_clip_as_multipart(monkeypatch, tmp_path):
    """P3: the wrapper builds the pinned multipart envelope from raw bytes."""
    client = _wrapper_client(monkeypatch, tmp_path)
    captured: dict = {}

    class _Response:
        status_code = 200
        content = b'{"status": 200, "message": "success", "data": {"file": "key.mp4"}}'
        headers = {"content-type": "application/json"}

        @property
        def text(self):
            return self.content.decode("utf-8")

    def fake_upstream(method, path, *, api_key, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["headers"] = kwargs.get("headers", {})
        captured["content"] = kwargs.get("content", b"")
        return _Response()

    monkeypatch.setattr("services.moneyprinter_service._upstream", fake_upstream)

    response = client.post(
        "/api/v1/video_materials",
        content=b"clip-bytes",
        headers={"x-api-key": "runtime-key", "x-vertep-filename": "scene-000.mp4"},
    )

    assert response.status_code == 200
    assert captured["path"] == "/api/v1/video_materials"
    assert captured["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    body = captured["content"]
    assert b'name="file"; filename="scene-000.mp4"' in body
    assert body.endswith(b"--\r\n")
    assert b"clip-bytes" in body


@pytest.mark.parametrize("filename", [
    "../escape.mp4",
    "sub/dir.mp4",
    'quote".mp4',
    "line\r\nbreak.mp4",
    "",
    "..",
])
def test_wrapper_refuses_an_unsafe_scene_clip_name(monkeypatch, tmp_path, filename):
    """A name that could break out of the multipart header is refused (§6)."""
    client = _wrapper_client(monkeypatch, tmp_path)

    response = client.post(
        "/api/v1/video_materials",
        content=b"clip-bytes",
        headers={"x-api-key": "runtime-key", "x-vertep-filename": filename},
    )

    assert response.status_code == 400


def test_wrapper_does_not_count_the_audio_generator_route_as_voice_staging():
    """P3: the pinned /audio route synthesises narration, so it is not staging."""
    import inspect as inspect_module

    from services import moneyprinter_service as wrapper

    source = inspect_module.getsource(wrapper.task_local_voice_capability)
    assert "_audio_upload_routes" in source
    assert "UploadFile" in inspect_module.getsource(wrapper._audio_upload_routes)


# ---------------------------------------------------------------------------
# Approved voice staging (Issue #122 §9.3 row 5, §9.6)
# ---------------------------------------------------------------------------


def _pinned_upstream(monkeypatch, tmp_path):
    """Install the pinned helpers the staging path depends on.

    Only the two functions of the pinned runtime that the staging uses are
    faked: ``utils.task_dir`` (which creates the task directory) and
    ``resolve_custom_audio_file`` (which only accepts a file inside it).
    """
    import types

    task_root = tmp_path / "storage" / "tasks"

    utils = types.ModuleType("app.utils.utils")
    utils.task_dir = lambda task_id: str(task_root / task_id)

    services = types.ModuleType("app.services")
    task = types.ModuleType("app.services.task")

    def resolve_custom_audio_file(task_id, file_name, allow_server_file_input=False):
        if allow_server_file_input:
            raise ValueError("server-side input is refused")
        candidate = Path(utils.task_dir(task_id)) / file_name
        if not candidate.is_file():
            raise FileNotFoundError(file_name)
        return str(candidate)

    task.resolve_custom_audio_file = resolve_custom_audio_file
    services.task = task

    app_module = types.ModuleType("app")
    app_module.utils = types.ModuleType("app.utils")
    app_module.utils.utils = utils
    app_module.services = services
    monkeypatch.setitem(__import__("sys").modules, "app", app_module)
    monkeypatch.setitem(__import__("sys").modules, "app.utils", app_module.utils)
    monkeypatch.setitem(__import__("sys").modules, "app.utils.utils", utils)
    monkeypatch.setitem(__import__("sys").modules, "app.services", services)
    monkeypatch.setitem(__import__("sys").modules, "app.services.task", task)
    monkeypatch.setenv("VERTEP_MONEYPRINTER_VOICE_DIR", str(tmp_path / "voice"))
    return task_root


def test_wrapper_stages_the_approved_voice_by_content_address(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)

    response = client.post(
        "/api/v1/voice",
        content=b"approved-voice-bytes",
        headers={"x-api-key": "runtime-key", "x-vertep-filename": "voice-0001.wav"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["voice"] == f"vertep-voice:{hashlib.sha256(b'approved-voice-bytes').hexdigest()}.wav"
    assert data["bytes"] == len(b"approved-voice-bytes")


@pytest.mark.parametrize("headers,content,expected", [
    ({"x-vertep-filename": "../escape.wav"}, b"x", 400),
    ({"x-vertep-filename": "sub/dir.wav"}, b"x", 400),
    ({"x-vertep-filename": "voice.exe"}, b"x", 400),
    ({"x-vertep-filename": ""}, b"x", 400),
    ({"x-vertep-filename": "voice.wav"}, b"", 400),
])
def test_wrapper_refuses_an_unusable_voice_staging_request(
    monkeypatch, tmp_path, headers, content, expected
):
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)

    response = client.post(
        "/api/v1/voice",
        content=content,
        headers={"x-api-key": "runtime-key", **headers},
    )

    assert response.status_code == expected


def _submit(client, *, submit_key: str | None = None, **overrides):
    payload = {
        "video_subject": "topic",
        "video_script": "approved narration",
        "video_source": "local",
        "video_materials": [{"provider": "local", "url": "scene-000.mp4", "duration": 2.0}],
        "custom_audio_file": "vertep-voice:" + "b" * 64 + ".wav",
        "video_aspect": "16:9",
    }
    payload.update(overrides)
    headers = {"x-api-key": "runtime-key"}
    if submit_key:
        headers["x-vertep-submit-key"] = submit_key
    return client.post("/api/v1/videos", json=payload, headers=headers)


def test_wrapper_places_the_staged_voice_in_the_task_directory(monkeypatch, tmp_path):
    """The approved voice becomes a task-local file before the pipeline runs."""
    client = _wrapper_client(monkeypatch, tmp_path)
    task_root = _pinned_upstream(monkeypatch, tmp_path)
    digest = hashlib.sha256(b"approved-voice-bytes").hexdigest()
    client.post(
        "/api/v1/voice",
        content=b"approved-voice-bytes",
        headers={"x-api-key": "runtime-key", "x-vertep-filename": "voice-0001.wav"},
    )
    captured: dict = {}

    def fake_upstream(method, path, *, api_key, **kwargs):
        captured.setdefault("calls", []).append((method, path, kwargs.get("content")))
        return httpx.Response(200, json={"status": 200, "message": "success", "data": {"task_id": "j-42"}})

    monkeypatch.setattr("services.moneyprinter_service._upstream", fake_upstream)

    response = _submit(client, custom_audio_file=f"vertep-voice:{digest}.wav")

    assert response.status_code == 200
    # The submit carries a task-local file name, never the CORE path or a marker.
    forwarded = json.loads(captured["calls"][0][2])
    assert forwarded["custom_audio_file"] == "vertep-voice.wav"
    staged = task_root / "j-42" / "vertep-voice.wav"
    assert staged.read_bytes() == b"approved-voice-bytes"
    assert not staged.with_suffix(".wav.part").exists()


def test_wrapper_never_asks_the_pinned_runtime_to_generate_or_publish(
    monkeypatch, tmp_path
):
    """P4/§9.6: Vertep owns script, narration and publication.

    The whole wrapper flow runs against a recording upstream: the narration is staged,
    every scene is uploaded, the approved script is submitted, the status is polled and
    the result is downloaded. The recording is the proof that not one of those steps
    used the pinned narration, script or publication capability — those belong to
    Vertep, and a runtime that quietly used them would render content nobody approved.
    """
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    digest = hashlib.sha256(b"approved-voice-bytes").hexdigest()
    client.post(
        "/api/v1/voice",
        content=b"approved-voice-bytes",
        headers={"x-api-key": "runtime-key", "x-vertep-filename": "voice-0001.wav"},
    )
    calls: list[tuple[str, str]] = []
    payloads: list[dict] = []

    def fake_upstream(method, path, *, api_key, **kwargs):
        calls.append((method, path))
        if path == "/api/v1/videos":
            payloads.append(json.loads(kwargs.get("content")))
            return httpx.Response(200, json={"status": 200, "message": "success",
                                             "data": {"task_id": "j-77"}})
        if path == "/api/v1/video_materials":
            return httpx.Response(200, json={"status": 200, "message": "success",
                                             "data": {"file": "scene-000.mp4"}})
        if path == "/api/v1/videos/vertep-j-77":
            return httpx.Response(200, json={"status": 200, "message": "success",
                                             "data": {"state": "completed", "file_url":
                                                      "final/j-77.mp4"}})
        if path.startswith("/api/v1/tasks/"):
            return httpx.Response(200, json={"status": 200, "message": "success",
                                             "data": {"state": "completed",
                                                      "file_url": "final/j-77.mp4"}})
        raise AssertionError(f"unexpected upstream call: {method} {path}")

    monkeypatch.setattr("services.moneyprinter_service._upstream", fake_upstream)
    uploaded = client.post(
        "/api/v1/video_materials",
        content=b"staged-scene-bytes",
        headers={"x-api-key": "runtime-key", "x-vertep-filename": "scene-000.mp4"},
    )
    assert uploaded.status_code == 200, uploaded.text
    submitted = _submit(client, submit_key="vertep-job-77-v1-abc123",
                        custom_audio_file=f"vertep-voice:{digest}.wav",
                        video_materials=[{"provider": "local", "url": "scene-000.mp4",
                                          "duration": 2.0}])
    assert submitted.status_code == 200, submitted.text
    polled = client.get("/api/v1/videos/vertep-job-77-v1-abc123",
                        headers={"x-api-key": "runtime-key"})
    assert polled.status_code == 200, polled.text

    forbidden = ("audio", "tts", "voice/synth", "llm", "gpt", "script", "prompt",
                 "publish", "upload", "youtube", "douyin", "tiktok")
    used = [f"{method} {path}" for method, path in calls]
    assert used, "the flow never reached the upstream, so nothing was proven"
    for entry in used:
        route = entry.split(" ", 1)[1].lower()
        assert not any(word in route for word in forbidden), (
            f"the wrapper used the pinned generation/publication route: {entry}")
    assert payloads[0]["video_script"] == "approved narration"
    assert payloads[0]["custom_audio_file"] == "vertep-voice.wav"


def test_wrapper_cancels_the_task_when_the_voice_cannot_be_placed(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: (
            calls.append((method, path)),
            httpx.Response(200, json={"status": 200, "message": "success", "data": {"task_id": "j-43"}}),
        )[1],
    )

    # Nothing was staged, so the wrapper cannot prove the approved voice.
    response = _submit(client)

    assert response.status_code == 503
    # The category tells CORE the approved voice was the reason, the cause tells an
    # operator which check failed — the attempt is never reported as merely "failed".
    assert response.json()["reason"] == "voice_staging_failed"
    assert response.json()["cause"] == "upstream_voice_staging_unsupported"
    assert [call[0] for call in calls] == ["POST", "DELETE"]
    assert calls[1][1] == "/api/v1/tasks/j-43"


def test_a_repeat_submit_after_a_restart_completes_the_voice_staging(monkeypatch, tmp_path):
    """Issue #122 P3/P6: the durable record outlives the process that staged the voice.

    A restart between writing the task mapping and staging the approved audio left a
    task that could only be narrated by the pinned TTS. The repeat submit now finishes
    the staging before it answers "reconciled", and refuses the key outright when it
    cannot.
    """
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    digest = hashlib.sha256(b"approved-voice-bytes").hexdigest()
    marker = f"vertep-voice:{digest}.wav"
    wrapper_module = __import__("services.moneyprinter_service", fromlist=["_remember_submit"])
    calls: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: (
            calls.append((method, path)),
            httpx.Response(200, json={"status": 200, "data": {"task_id": "j-79"}}),
        )[1],
    )
    # A record written by a process that died before the voice reached the task dir.
    wrapper_module._remember_submit("job-1-v1-restart", task_id="j-79", marker=marker,
                                    state="submitted")

    refused = _submit_with_key(client, "job-1-v1-restart", custom_audio_file=marker)

    assert refused.status_code == 503
    assert refused.json()["reason"] == "voice_staging_failed"
    assert ("DELETE", "/api/v1/tasks/j-79") in calls, \
        "an attempt that cannot be narrated by the approved voice is aborted"

    # The voice bytes are staged now, so the same key is answered from the record
    # instead of creating a second upstream task.
    client.post(
        "/api/v1/voice",
        content=b"approved-voice-bytes",
        headers={"x-api-key": "runtime-key", "x-vertep-filename": "voice-0001.wav"},
    )
    reconciled = _submit_with_key(client, "job-1-v1-restart", custom_audio_file=marker)

    assert reconciled.status_code == 409, "a refused key is terminal, not retried"
    assert reconciled.json()["reason"] == "voice_staging_failed"


def test_a_corrupted_submit_record_is_never_treated_as_a_new_submit(monkeypatch, tmp_path):
    """Issue #122 P6: an unreadable record fails closed instead of resubmitting."""
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    wrapper_module = __import__("services.moneyprinter_service",
                                fromlist=["_submit_record_path"])
    path = wrapper_module._submit_record_path("job-1-v1-broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, route, *, api_key, **kwargs: (
            calls.append((method, route)),
            httpx.Response(200, json={"status": 200, "data": {"task_id": "j-80"}}),
        )[1],
    )

    response = _submit_with_key(client, "job-1-v1-broken")

    assert response.status_code == 503
    assert response.json()["reason"] == "submit_record_unreadable"
    assert calls == [], "a corrupted record must never become a second upstream submit"


def test_a_submit_record_that_belongs_to_another_key_is_refused(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    wrapper_module = __import__("services.moneyprinter_service",
                                fromlist=["_submit_record_path"])
    path = wrapper_module._submit_record_path("job-1-v1-other")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"submit_key": "job-1-v1-mine", "task_id": "j-81",
                                "state": "staged"}), encoding="utf-8")

    response = _submit_with_key(client, "job-1-v1-other")

    assert response.status_code == 503
    assert response.json()["reason"] == "submit_record_unreadable"


@pytest.mark.parametrize("overrides,expected", [
    ({"video_script": "  "}, 400),
    ({"custom_audio_file": ""}, 400),
    ({"custom_audio_file": "/opt/vertep/storage/voice.wav"}, 400),
    ({"video_materials": []}, 400),
    ({"video_materials": [{"provider": "local", "url": "/opt/vertep/scene.png"}]}, 400),
    ({"video_materials": [{"provider": "local", "url": "https://example/scene.mp4"}]}, 400),
    ({"bgm_file": "https://example/music.mp3"}, 400),
])
def test_wrapper_refuses_a_submit_that_would_run_its_own_generation(
    monkeypatch, tmp_path, overrides, expected
):
    """§9.6: the wrapper refuses anything that could add narration or a fetch."""
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    called: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda *args, **kwargs: called.append(args) or None,
    )

    response = _submit(client, **overrides)

    assert response.status_code == expected
    assert called == []


def test_wrapper_voice_staging_capability_never_uses_the_audio_generator(
    monkeypatch, tmp_path
):
    """The capability is derived from the wrapper's own staging, not from TTS."""
    from services import moneyprinter_service as wrapper

    _pinned_upstream(monkeypatch, tmp_path)
    monkeypatch.setattr(wrapper, "_audio_upload_routes", lambda: [])

    capability = wrapper.task_local_voice_capability()

    assert capability["task_local_voice"] is True
    assert capability["task_local_resolution_only"] is True
    assert capability["routes"] == [wrapper.VOICE_STAGING_ROUTE]


def _submit_with_key(client, submit_key: str, **overrides):
    payload = {
        "video_subject": "topic",
        "video_script": "approved narration",
        "video_source": "local",
        "video_materials": [{"provider": "local", "url": "scene-000.mp4", "duration": 2.0}],
        "custom_audio_file": "vertep-voice:" + "b" * 64 + ".wav",
        "video_aspect": "16:9",
    }
    payload.update(overrides)
    return client.post(
        "/api/v1/videos", json=payload,
        headers={"x-api-key": "runtime-key", "x-vertep-submit-key": submit_key},
    )


def test_wrapper_records_the_submit_key_before_contacting_the_upstream(
    monkeypatch, tmp_path
):
    """The durable record exists even when the upstream call never answered."""
    client = _wrapper_client(monkeypatch, tmp_path)
    task_root = _pinned_upstream(monkeypatch, tmp_path)
    digest = hashlib.sha256(b"approved-voice-bytes").hexdigest()
    client.post(
        "/api/v1/voice",
        content=b"approved-voice-bytes",
        headers={"x-api-key": "runtime-key", "x-vertep-filename": "voice-0001.wav"},
    )
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        _raises(httpx.ConnectError("runtime is unreachable")),
    )

    with pytest.raises(httpx.ConnectError):
        _submit_with_key(client, "job-1-v1-abc", custom_audio_file=f"vertep-voice:{digest}.wav")

    record = json.loads((tmp_path / "submits" / "job-1-v1-abc.json").read_text(encoding="utf-8"))
    assert record["submit_key"] == "job-1-v1-abc"
    assert record["task_id"] is None
    assert record["state"] == "submitting"


def test_wrapper_reconciles_a_lost_response_through_the_submit_key(
    monkeypatch, tmp_path
):
    """A repeat submit with the same key is answered from the record."""
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    digest = hashlib.sha256(b"approved-voice-bytes").hexdigest()
    client.post(
        "/api/v1/voice",
        content=b"approved-voice-bytes",
        headers={"x-api-key": "runtime-key", "x-vertep-filename": "voice-0001.wav"},
    )
    calls: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: (
            calls.append((method, path)),
            httpx.Response(200, json={"status": 200, "message": "success",
                                       "data": {"task_id": "j-77"}}),
        )[1],
    )

    first = _submit_with_key(client, "job-1-v1-abc",
                             custom_audio_file=f"vertep-voice:{digest}.wav")
    second = _submit_with_key(client, "job-1-v1-abc",
                              custom_audio_file=f"vertep-voice:{digest}.wav")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["message"] == "reconciled"
    assert second.json()["data"]["task_id"] == "j-77"
    # The upstream saw exactly one submit for that key.
    assert [call for call in calls if call[0] == "POST"] == [
        ("POST", "/api/v1/videos")
    ]

    status = client.get("/api/v1/videos/job-1-v1-abc", headers={"x-api-key": "runtime-key"})
    # The approved voice is verified inside the pinned task directory, so the record is
    # not merely "submitted": a repeat submit can be answered as fully reconciled.
    # ``runtime_state`` is resolved from the upstream task; this stand-in answers every
    # request like a submit, so the real state of the task cannot be read from it.
    assert status.json() == {"status": 200, "state": "submitted",
                             "record_state": "staged", "voice_staged": True,
                             "runtime_state": "unreachable",
                             "data": {"task_id": "j-77"}}


def test_wrapper_refuses_a_repeat_submit_while_the_first_is_in_flight(
    monkeypatch, tmp_path
):
    """An in-flight key is never answered with a second upstream submit."""
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: (
            calls.append((method, path)),
            httpx.Response(200, json={"status": 200, "data": {"task_id": "j-78"}}),
        )[1],
    )
    # The record of an attempt that never learned its task id.
    wrapper_module = __import__("services.moneyprinter_service", fromlist=["_remember_submit"])
    wrapper_module._remember_submit("job-1-v1-abc", task_id=None)

    response = _submit_with_key(client, "job-1-v1-abc")

    assert response.status_code == 409
    assert response.json()["reason"] == "upstream_submit_unknown"
    assert calls == []

    status = client.get("/api/v1/videos/job-1-v1-abc", headers={"x-api-key": "runtime-key"})
    assert status.json()["state"] == "unknown"


def test_wrapper_reports_an_unknown_submit_key_as_unknown(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path)

    status = client.get("/api/v1/videos/never-submitted", headers={"x-api-key": "runtime-key"})

    assert status.status_code == 404
    assert status.json()["state"] == "unknown"


def test_a_definitively_refused_submit_releases_its_key_for_a_retry(monkeypatch, tmp_path):
    """A 4xx submit created no upstream task, so the attempt stays retryable.

    Keeping the key reserved after a definitive refusal would turn one rejected submit
    into a permanently blocked attempt, and a 5xx must not release it: the upstream may
    already hold the work.
    """
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: (
            calls.append((method, path)),
            httpx.Response(422, json={"status": 422, "message": "unsupported parameter"}),
        )[1],
    )

    refused = _submit_with_key(client, "job-1-v1-refused")

    assert refused.status_code == 422
    assert len(calls) == 1
    # The key is free again: a retry reaches the upstream instead of being blocked.
    retried = _submit_with_key(client, "job-1-v1-refused")
    assert retried.status_code == 422
    assert len(calls) == 2

    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: (
            calls.append((method, path)),
            httpx.Response(503, text="Service Unavailable"),
        )[1],
    )
    assert _submit_with_key(client, "job-1-v1-ambiguous").status_code == 503
    # A server-side failure leaves the key reserved, so a retry is reported as unknown
    # instead of risking a second upstream task.
    calls.clear()
    again = _submit_with_key(client, "job-1-v1-ambiguous")
    assert again.status_code == 409
    assert again.json()["reason"] == "upstream_submit_unknown"
    assert calls == []


def test_wrapper_aborts_an_attempt_by_its_durable_submit_key(monkeypatch, tmp_path):
    """Cancellation reaches the upstream task the key was resolved to."""
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    wrapper_module = __import__("services.moneyprinter_service", fromlist=["_remember_submit"])
    wrapper_module._remember_submit("job-1-v1-abc", task_id="j-79")
    calls: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: (
            calls.append((method, path)),
            httpx.Response(204),
        )[1],
    )

    response = client.delete("/api/v1/videos/job-1-v1-abc", headers={"x-api-key": "runtime-key"})

    assert response.status_code == 204
    assert calls == [("DELETE", "/api/v1/tasks/j-79")]


def test_wrapper_abort_reports_a_busy_attempt_as_not_cancelled(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    wrapper_module = __import__("services.moneyprinter_service", fromlist=["_remember_submit"])
    wrapper_module._remember_submit("job-1-v1-abc", task_id="j-80")
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: httpx.Response(
            409, json={"status": 409, "message": "task is still running"},
        ),
    )

    response = client.delete("/api/v1/videos/job-1-v1-abc", headers={"x-api-key": "runtime-key"})

    assert response.status_code == 409


def test_wrapper_abort_of_an_unknown_key_is_not_cancelled(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: calls.append((method, path)) or httpx.Response(204),
    )

    response = client.delete("/api/v1/videos/never-submitted", headers={"x-api-key": "runtime-key"})

    assert response.status_code == 404
    assert calls == []


def test_wrapper_reports_no_voice_capability_without_the_pinned_resolver(monkeypatch, tmp_path):
    from services import moneyprinter_service as wrapper

    import builtins

    real_import = builtins.__import__

    def refuse_app(name, *args, **kwargs):
        if name.startswith("app."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_app)
    monkeypatch.delitem(__import__("sys").modules, "app.services.task", raising=False)

    capability = wrapper.task_local_voice_capability()

    assert capability["task_local_voice"] is False
    assert capability["reason"] == "upstream_schema_unsupported"


# ---------------------------------------------------------------------------
# P6 §5/§9.8: the real state of a cancelled attempt is reported, never assumed
# ---------------------------------------------------------------------------


def _status_of(client, submit_key: str):
    return client.get(f"/api/v1/videos/{submit_key}", headers={"x-api-key": "runtime-key"})


def test_the_runtime_state_of_a_submitted_attempt_is_reported_from_the_upstream(
        monkeypatch, tmp_path):
    """A logical cancel proves nothing about compute, so the state is resolved (§9.8)."""
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    wrapper_module = __import__("services.moneyprinter_service", fromlist=["_remember_submit"])
    wrapper_module._remember_submit("job-1-v1-state", task_id="j-81")
    seen: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: (
            seen.append((method, path)),
            httpx.Response(200, json={"status": 200, "data": {"state": 4}}),
        )[1],
    )

    body = _status_of(client, "job-1-v1-state").json()

    assert body["state"] == "submitted"
    assert body["runtime_state"] == "running", "a busy upstream task is still rendering"
    assert seen == [("GET", "/api/v1/tasks/j-81")]


@pytest.mark.parametrize("upstream_state, expected", [
    (1, "finished"),
    (-1, "failed"),
    (4, "running"),
])
def test_every_upstream_task_state_is_mapped(monkeypatch, tmp_path, upstream_state, expected):
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    wrapper_module = __import__("services.moneyprinter_service", fromlist=["_remember_submit"])
    wrapper_module._remember_submit("job-1-v1-map", task_id="j-82")
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: httpx.Response(
            200, json={"status": 200, "data": {"state": upstream_state}}),
    )

    assert _status_of(client, "job-1-v1-map").json()["runtime_state"] == expected


def test_a_task_the_upstream_no_longer_knows_reports_absent(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    wrapper_module = __import__("services.moneyprinter_service", fromlist=["_remember_submit"])
    wrapper_module._remember_submit("job-1-v1-gone", task_id="j-83")
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: httpx.Response(404, json={"status": 404}),
    )

    assert _status_of(client, "job-1-v1-gone").json()["runtime_state"] == "absent"


def test_an_unreachable_upstream_is_unreachable_and_never_absent(monkeypatch, tmp_path):
    """Silence must never be read as "stopped" (§5)."""
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    wrapper_module = __import__("services.moneyprinter_service", fromlist=["_remember_submit"])
    wrapper_module._remember_submit("job-1-v1-silent", task_id="j-84")
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        _raises(httpx.ConnectError("upstream unreachable")),
    )

    body = _status_of(client, "job-1-v1-silent").json()

    assert body["state"] == "submitted"
    assert body["runtime_state"] == "unreachable"


def test_an_unreadable_upstream_body_is_unreachable(monkeypatch, tmp_path):
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    wrapper_module = __import__("services.moneyprinter_service", fromlist=["_remember_submit"])
    wrapper_module._remember_submit("job-1-v1-garbage", task_id="j-85")
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: httpx.Response(200, text="not json"),
    )

    assert _status_of(client, "job-1-v1-garbage").json()["runtime_state"] == "unreachable"


def test_a_submit_without_an_upstream_task_reports_absent(monkeypatch, tmp_path):
    """A refused submit never created upstream work, so nothing of it is running."""
    client = _wrapper_client(monkeypatch, tmp_path)
    _pinned_upstream(monkeypatch, tmp_path)
    wrapper_module = __import__("services.moneyprinter_service", fromlist=["_remember_submit"])
    wrapper_module._remember_submit("job-1-v1-refused", task_id=None)
    calls: list = []
    monkeypatch.setattr(
        "services.moneyprinter_service._upstream",
        lambda method, path, *, api_key, **kwargs: calls.append(path) or httpx.Response(204),
    )

    body = _status_of(client, "job-1-v1-refused").json()

    assert body["state"] == "unknown"
    assert body["runtime_state"] == "absent"
    assert calls == [], "an attempt without an upstream task needs no upstream call"
