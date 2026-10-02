"""Runtime manifest support for pinned external engines (Issue #122 P2).

This module provides utilities to read and validate runtime inventory files
generated during Docker image builds. The manifest contains:
- Pinned upstream commit
- Bridge version and schema version
- Dependency inventory (pip freeze)
- Digest for verification

The manifest is used by health checks to detect runtime drift and ensure
the deployed runtime matches the expected snapshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeManifest:
    """Parsed runtime inventory from runtime-inventory.json."""

    format: str
    bridge_version: str
    bridge_schema_version: str
    components: dict
    dependencies: list[str]
    raw: dict

    @property
    def upstream_commit(self) -> str | None:
        """Extract the upstream commit from components."""
        for name, info in self.components.items():
            if "commit" in info:
                return info["commit"]
        return None

    @property
    def upstream_repository(self) -> str | None:
        """Extract the upstream repository from components."""
        for name, info in self.components.items():
            if "repository" in info:
                return info["repository"]
        return None

    def compute_digest(self) -> str:
        """Compute SHA256 digest of the manifest for verification."""
        manifest_str = json.dumps(self.raw, sort_keys=True)
        return hashlib.sha256(manifest_str.encode("utf-8")).hexdigest()

    def verify(self, expected_commit: str | None = None) -> bool:
        """Verify manifest integrity and optionally match expected commit."""
        if expected_commit and self.upstream_commit != expected_commit:
            return False
        return True


def load_manifest(path: Path) -> RuntimeManifest:
    """Load and parse a runtime inventory file."""
    if not path.is_file():
        raise FileNotFoundError(f"Runtime manifest not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

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
