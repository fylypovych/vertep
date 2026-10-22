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
        self.name = name
        self.version = self._validate_version(version)
        self.capabilities = capabilities or []
        self.dependencies = dependencies or []
        self.resource_requirements = resource_requirements or {}
        self.description = description

    @staticmethod
    def _validate_version(version: str) -> str:
        """Validate semver format A.B.C.D."""
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", version):
            raise ValueError(f"Invalid version format: {version}. Expected A.B.C.D")
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