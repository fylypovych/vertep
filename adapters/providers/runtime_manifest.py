"""Pinned runtime inventory for the isolated MoneyPrinterTurbo engine.

Issue #122 P2 requires the external engine runtime to be pinned by a manifest
that readiness checks can verify. The image build records what it was actually
built from; every consumer must re-check those values instead of trusting build
arguments or assuming the upstream API matches the contract.

Two layers live here on purpose:

* :class:`RuntimeManifest`, :func:`load_manifest` and :func:`generate_sbom`
  parse an inventory document and never judge it. They are the stable API used by
  the health check, the container self-test and the SBOM tooling.
* :func:`validate_inventory` / :func:`verify_inventory` apply the fail-closed
  rules of Issue #122: a missing, unreadable, malformed or drifted inventory
  raises :class:`RuntimeManifestError` and the caller must refuse work rather
  than degrade to the native engine.

The inventory is content-addressed (``file_digests``), so editing a pinned file
inside the image is detectable.

The pin itself lives here, not in ``video_engines``: the container copies only
``adapters`` and ``services``, so anything the wrapper needs at runtime has to
stay importable without the rest of the platform (see
``services.moneyprinter_service._contract``). ``MONEY_PRINTER_CONTRACT`` reads
the pin from this module, so there is exactly one place to bump.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

INVENTORY_FORMAT = "moneyprinter_runtime/v1"
PINNED_UPSTREAM_REPOSITORY = "harry0703/MoneyPrinterTurbo"
PINNED_UPSTREAM_COMMIT = "2e1b30396e059e55939cc802c60faac2061e4d41"
PINNED_UPSTREAM_REFERENCE = f"{PINNED_UPSTREAM_REPOSITORY}@{PINNED_UPSTREAM_COMMIT}"
INVENTORY_REPOSITORY = PINNED_UPSTREAM_REPOSITORY
INVENTORY_COMPONENT = "moneyprinter"
DEFAULT_INVENTORY_PATH = "/opt/moneyprinter/runtime-inventory.json"
INVENTORY_PATH_ENV = "MONEYPRINTER_RUNTIME_INVENTORY"

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_DEPENDENCY = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*==[0-9A-Za-z][0-9A-Za-z.*+!-]*$")


class RuntimeManifestError(RuntimeError):
    """Raised when the pinned MoneyPrinterTurbo runtime cannot be verified."""


@dataclass(frozen=True)
class RuntimeManifest:
    """Parsed runtime inventory from ``runtime-inventory.json``."""

    format: str
    bridge_version: str
    bridge_schema_version: str
    components: dict
    dependencies: list
    raw: dict

    @property
    def upstream_commit(self) -> str | None:
        """Extract the upstream commit from the declared components."""
        for info in self.components.values():
            if isinstance(info, dict) and info.get("commit"):
                return info["commit"]
        return None

    @property
    def upstream_repository(self) -> str | None:
        """Extract the upstream repository from the declared components."""
        for info in self.components.values():
            if isinstance(info, dict) and info.get("repository"):
                return info["repository"]
        return None

    @property
    def upstream_version(self) -> str | None:
        for info in self.components.values():
            if isinstance(info, dict) and info.get("version"):
                return info["version"]
        return None

    @property
    def upstream_reference(self) -> str | None:
        repository, commit = self.upstream_repository, self.upstream_commit
        if repository and commit:
            return f"{repository}@{commit}"
        return None

    @property
    def file_digests(self) -> dict:
        digests = self.raw.get("file_digests")
        return digests if isinstance(digests, dict) else {}

    @property
    def lock_digest(self) -> str:
        """Digest of the inventory content itself."""
        return self.compute_digest()

    def compute_digest(self) -> str:
        """Compute the SHA256 digest of the manifest for verification."""
        payload = json.dumps(self.raw, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def verify(self, expected_commit: str | None = None) -> bool:
        """Verify manifest integrity and optionally match the expected commit."""
        if expected_commit and self.upstream_commit != expected_commit:
            return False
        return True

    def verify_file_digests(self, root: Path) -> None:
        """Re-hash the pinned files this inventory claims to describe."""
        for name, expected in sorted(self.file_digests.items()):
            target = Path(root) / name
            if not target.is_file():
                raise RuntimeManifestError(f"pinned runtime file is missing: {name}")
            observed = hashlib.sha256(target.read_bytes()).hexdigest()
            if observed != expected:
                raise RuntimeManifestError(f"pinned runtime file was modified: {name}")


def inventory_path(path: Path | str | None = None) -> Path:
    """Resolve the inventory location without silently defaulting to a guess."""
    if path is not None:
        return Path(path)
    configured = os.getenv(INVENTORY_PATH_ENV, "").strip()
    if configured:
        return Path(configured)
    return Path(DEFAULT_INVENTORY_PATH)


def build_inventory(*, repository: str, commit: str, version: str,
                    bridge_version: str, bridge_schema_version: str,
                    dependencies: list, file_digests: dict) -> dict:
    """Build the inventory payload written into the image."""
    return {
        "format": INVENTORY_FORMAT,
        "components": {
            INVENTORY_COMPONENT: {
                "repository": repository,
                "commit": commit,
                "version": version,
            }
        },
        "bridge_version": bridge_version,
        "bridge_schema_version": bridge_schema_version,
        "dependencies": sorted(dependencies),
        "file_digests": dict(sorted(file_digests.items())),
    }


def load_manifest(path: Path | str | None = None) -> RuntimeManifest:
    """Load and parse a runtime inventory file."""
    target = Path(path) if path is not None else Path(DEFAULT_INVENTORY_PATH)
    if not target.is_file():
        raise FileNotFoundError(f"Runtime manifest not found: {target}")

    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Runtime manifest is not a JSON object")

    return RuntimeManifest(
        format=data.get("format", "unknown"),
        bridge_version=data.get("bridge_version", "unknown"),
        bridge_schema_version=data.get("bridge_schema_version", "unknown"),
        components=data.get("components", {}),
        dependencies=data.get("dependencies", []),
        raw=data,
    )


def load_inventory(path: Path | str | None = None) -> RuntimeManifest:
    """Load the inventory, reporting absence as a hard verification failure."""
    target = inventory_path(path)
    if not target.is_file():
        raise RuntimeManifestError(f"pinned runtime inventory is missing: {target}")
    try:
        return load_manifest(target)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeManifestError(
            f"pinned runtime inventory is unreadable: {target}"
        ) from error


def validate_inventory(manifest: RuntimeManifest, *, root: Path | None = None,
                       expected_commit: str | None = None,
                       expected_bridge_version: str | None = None,
                       expected_bridge_schema_version: str | None = None) -> RuntimeManifest:
    """Apply the fail-closed rules of Issue #122 to a parsed inventory.

    Raises :class:`RuntimeManifestError` on the first violation so that callers
    cannot accidentally continue with an unverified runtime.
    """
    if manifest.format != INVENTORY_FORMAT:
        raise RuntimeManifestError(
            f"pinned runtime inventory must use format {INVENTORY_FORMAT!r}"
        )

    component = manifest.components.get(INVENTORY_COMPONENT)
    if not isinstance(component, dict):
        raise RuntimeManifestError("pinned runtime inventory declares no moneyprinter component")

    if component.get("repository") != INVENTORY_REPOSITORY:
        raise RuntimeManifestError(
            f"pinned runtime must come from {INVENTORY_REPOSITORY!r}, "
            f"found {component.get('repository')!r}"
        )

    commit = component.get("commit")
    if not isinstance(commit, str) or not _COMMIT.match(commit):
        raise RuntimeManifestError("pinned moneyprinter commit is not a 40-hex sha")

    version = component.get("version")
    if not isinstance(version, str) or not _VERSION.match(version):
        raise RuntimeManifestError("pinned moneyprinter version is malformed")

    for key, value in (("bridge_version", manifest.bridge_version),
                       ("bridge_schema_version", manifest.bridge_schema_version)):
        if not isinstance(value, str) or not _VERSION.match(value):
            raise RuntimeManifestError(f"pinned runtime {key} is malformed")

    dependencies = manifest.dependencies
    if not isinstance(dependencies, list) or not dependencies:
        raise RuntimeManifestError("pinned runtime inventory declares no dependency inventory")
    for item in dependencies:
        if not isinstance(item, str) or not _DEPENDENCY.match(item):
            raise RuntimeManifestError(f"dependency inventory entry is not pinned: {item!r}")

    file_digests = manifest.file_digests
    if not file_digests:
        raise RuntimeManifestError("pinned runtime inventory declares no file digests")
    for name, digest in file_digests.items():
        if not isinstance(name, str) or not name or "/" in name or name.startswith("."):
            raise RuntimeManifestError(f"pinned runtime file name is invalid: {name!r}")
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise RuntimeManifestError(f"pinned runtime digest is malformed for {name!r}")

    if expected_commit is not None and commit != expected_commit:
        raise RuntimeManifestError(
            f"pinned runtime commit drift: expected {expected_commit}, found {commit}"
        )
    if expected_bridge_version is not None and manifest.bridge_version != expected_bridge_version:
        raise RuntimeManifestError(
            "pinned runtime bridge version drift: expected "
            f"{expected_bridge_version}, found {manifest.bridge_version}"
        )
    if (expected_bridge_schema_version is not None
            and manifest.bridge_schema_version != expected_bridge_schema_version):
        raise RuntimeManifestError(
            "pinned runtime bridge schema drift: expected "
            f"{expected_bridge_schema_version}, found {manifest.bridge_schema_version}"
        )

    manifest.verify_file_digests(
        Path(root) if root is not None else inventory_path().parent
    )
    return manifest


def verify_inventory(path: Path | str | None = None, *, root: Path | None = None,
                     expected_commit: str | None = None,
                     expected_bridge_version: str | None = None,
                     expected_bridge_schema_version: str | None = None) -> RuntimeManifest:
    """Load and fully verify the inventory, or raise.

    Every mismatch is a hard failure: the caller must refuse the engine instead of
    degrading to the native engine or reporting itself ready.
    """
    return validate_inventory(
        load_inventory(path),
        root=root,
        expected_commit=expected_commit,
        expected_bridge_version=expected_bridge_version,
        expected_bridge_schema_version=expected_bridge_schema_version,
    )


def installed_dependencies() -> tuple:
    """Report the dependency inventory actually installed in this process."""
    from importlib.metadata import distributions

    return tuple(sorted(
        f"{dist.metadata['Name']}=={dist.version}"
        for dist in distributions()
        if dist.metadata["Name"]
    ))


def dependency_drift(manifest: RuntimeManifest) -> list:
    """Return installed packages that are not present in the pinned inventory."""
    declared = set(manifest.dependencies or ())
    return sorted(package for package in installed_dependencies() if package not in declared)


def generate_sbom(manifest: RuntimeManifest) -> dict:
    """Generate a minimal SBOM from the runtime manifest."""
    return {
        "format": "sbom/v1",
        "runtime_format": manifest.format,
        "bridge_version": manifest.bridge_version,
        "bridge_schema_version": manifest.bridge_schema_version,
        "components": [
            {
                "name": name,
                "version": info.get("version", "unknown"),
                "commit": info.get("commit"),
                "repository": info.get("repository"),
            }
            for name, info in manifest.components.items()
        ],
        "dependencies": manifest.dependencies,
        "digest": manifest.compute_digest(),
    }
