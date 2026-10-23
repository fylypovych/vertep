"""Module Registry - CORE-side module management.

Maintains registry of installed modules and their capabilities.
Provides API for module discovery, registration, and lifecycle management.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .manifest import ModuleManifest


class ModuleRegistry:
    """Registry of installed modules on CORE."""

    def __init__(self) -> None:
        self._modules: Dict[str, ModuleManifest] = {}
        self._capability_index: Dict[str, List[str]] = {}  # capability -> [module_names]

    def register_module(self, manifest: ModuleManifest, force: bool = False) -> bool:
        """Register a module in the registry.

        Args:
            manifest: ModuleManifest to register
            force: If True, overwrite existing module with same name

        Returns:
            True if registration succeeded
        """
        name = manifest.name

        # Check dependencies
        for dep in manifest.dependencies:
            if dep not in self._modules and not self._is_system_dependency(dep):
                return False  # Dependency not met

        # Check version compatibility
        if name in self._modules and not force:
            existing = self._modules[name]
            # Simple version check - in production would use semver comparison
            if existing.version == manifest.version:
                return False  # Already registered with same version

        self._modules[name] = manifest

        # Update capability index
        for cap in manifest.capabilities:
            if cap not in self._capability_index:
                self._capability_index[cap] = []
            if name not in self._capability_index[cap]:
                self._capability_index[cap].append(name)

        return True

    def unregister_module(self, name: str) -> bool:
        """Unregister a module from the registry."""
        if name not in self._modules:
            return False

        manifest = self._modules.pop(name)

        # Remove from capability index
        for cap in manifest.capabilities:
            if cap in self._capability_index:
                self._capability_index[cap] = [
                    m for m in self._capability_index[cap] if m != name
                ]
                if not self._capability_index[cap]:
                    del self._capability_index[cap]

        return True

    def get_module(self, name: str) -> Optional[ModuleManifest]:
        """Get a module by name."""
        return self._modules.get(name)

    def list_modules(self) -> List[ModuleManifest]:
        """List all registered modules."""
        return list(self._modules.values())

    def get_capabilities(self, capability: str) -> List[str]:
        """Get list of module names that provide a capability."""
        return self._capability_index.get(capability, [])

    def _is_system_dependency(self, dep: str) -> bool:
        """Check if dependency is a system-level dependency."""
        system_deps = {"core_runtime", "python_runtime", "docker_runtime", "gpu_runtime"}
        return dep in system_deps

    def has_capability(self, capability: str) -> bool:
        """Check if any registered module provides the given capability."""
        return capability in self._capability_index