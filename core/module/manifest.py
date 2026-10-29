"""Module Manifest - definition of installed modules.

Represents a module with its capabilities, dependencies, and resource requirements.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional


class ModuleManifest:
    """A module manifest describing capabilities and requirements."""

    def __init__(
        self,
        name: str,
        version: str,
        capabilities: List[str],
        dependencies: List[str],
        resource_requirements: Dict[str, Any],
        description: str = "",
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Module name must be a nonempty string")
        if not isinstance(capabilities, list) or any(not isinstance(cap, str) or not cap for cap in capabilities):
            raise ValueError("Capabilities must be nonempty strings")
        if not isinstance(dependencies, list):
            raise ValueError("Dependencies must be a list")
        for dep in dependencies:
            if isinstance(dep, str) and dep:
                continue
            if not isinstance(dep, dict) or not isinstance(dep.get("name"), str) or not dep["name"]:
                raise ValueError("Invalid module dependency")
            for field in ("min_version", "max_version"):
                if field in dep:
                    self._validate_version(dep[field])
        if not isinstance(resource_requirements, dict):
            raise ValueError("Resource requirements must be an object")
        for key in ("min_vram_mb", "min_cpu_cores", "min_ram_mb"):
            value = resource_requirements.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Invalid module resource requirement: {key}")
        self.name = name
        self.version = self._validate_version(version)
        self.capabilities = capabilities or []
        self.dependencies = dependencies or []
        self.resource_requirements = resource_requirements or {}
        self.description = description

    @staticmethod
    def _validate_version(version: str) -> str:
        """Validate semver format A.B.C.D."""
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", version):
            raise ValueError(f"Invalid version format: {version}. Expected SemVer or legacy A.B.C.D")
        return version

    def to_dict(self) -> dict:
        """Convert manifest to dictionary for serialization."""
        return {
            "name": self.name,
            "version": self.version,
            "capabilities": self.capabilities,
            "dependencies": self.dependencies,
            "resource_requirements": self.resource_requirements,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModuleManifest":
        """Create manifest from dictionary."""
        data = data.get("manifest", data)
        return cls(
            name=data["name"],
            version=data["version"],
            capabilities=data.get("capabilities", []),
            dependencies=data.get("dependencies", []),
            resource_requirements=data.get("resource_requirements", {}),
            description=data.get("description", ""),
        )

    def __repr__(self) -> str:
        return f"ModuleManifest(name={self.name}, version={self.version})"
