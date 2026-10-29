"""Module Registry - CORE-side module management.

Maintains registry of installed modules and their capabilities.
Provides API for module discovery, registration, and lifecycle management.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .manifest import ModuleManifest


class ModuleRegistry:
    """Registry of installed modules on CORE."""

    def __init__(self, system_versions: dict | None = None) -> None:
        self._modules: Dict[str, ModuleManifest] = {}
        self._capability_index: Dict[str, List[str]] = {}  # capability -> [module_names]
        self.system_versions = system_versions or {}

    def register_module(self, manifest: ModuleManifest, force: bool = False) -> bool:
        """Register a module in the registry.

        Args:
            manifest: ModuleManifest to register
            force: If True, overwrite existing module with same name

        Returns:
            True if registration succeeded
        """
        name = manifest.name
        graph = {key: [dep.get("name") if isinstance(dep, dict) else dep for dep in value.dependencies]
                 for key, value in {**self._modules, name: manifest}.items()}
        visiting, visited = set(), set()

        def cyclic(node):
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(cyclic(dep) for dep in graph.get(node, [])):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        if cyclic(name):
            return False

        # Check dependencies
        for dep in manifest.dependencies:
            dependency = dep if isinstance(dep, dict) else {"name": dep}
            dep_name = dependency["name"]
            if dep_name == name:
                return False
            if dep_name not in self._modules and not self._is_system_dependency(dep_name):
                return False  # Dependency not met
            from packaging.version import Version
            version = self._modules[dep_name].version if dep_name in self._modules else self.system_versions.get(dep_name)
            if dependency.get("min_version") or dependency.get("max_version"):
                if version is None:
                    return False
                if dependency.get("min_version") and Version(version) < Version(dependency["min_version"]):
                    return False
                if dependency.get("max_version") and Version(version) > Version(dependency["max_version"]):
                    return False

        # Check version compatibility
        if name in self._modules and not force:
            existing = self._modules[name]
            # Simple version check - in production would use semver comparison
            if existing.version == manifest.version:
                return False  # Already registered with same version

        # Replacement must invalidate capabilities removed by the new version.
        if name in self._modules:
            old = self._modules[name]
            for cap in old.capabilities:
                self._capability_index[cap] = [module for module in self._capability_index.get(cap, []) if module != name]
                if not self._capability_index[cap]:
                    self._capability_index.pop(cap)
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
        for other in self._modules.values():
            if other.name != name and any((dep.get("name") if isinstance(dep, dict) else dep) == name for dep in other.dependencies):
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
