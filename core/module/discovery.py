"""Module Discovery - automatic module detection on workers.

Handles worker-side capability reporting and CORE-side capability caching.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .manifest import ModuleManifest


class ModuleDiscovery:
    """Manages module discovery between CORE and workers."""

    def __init__(self, registry: Optional[Any] = None) -> None:
        self.registry = registry  # Would be ModuleRegistry instance
        self._cached_capabilities: Dict[str, List[str]] = {}

    def report_worker_capabilities(
        self, worker_id: str, capabilities: List[str], resources: Dict[str, Any]
    ) -> None:
        """Process capability report from a registering worker.

        Args:
            worker_id: Identifier of the worker reporting capabilities
            capabilities: List of module capability names the worker supports
            resources: Resource information (vram_mb, cpu_cores, etc.)
        """
        # Update internal cache
        for cap in capabilities:
            if cap not in self._cached_capabilities:
                self._cached_capabilities[cap] = []
            if worker_id not in self._cached_capabilities[cap]:
                self._cached_capabilities[cap].append(worker_id)

    def get_available_modules(
        self, required_capabilities: List[str]
    ) -> Dict[str, List[str]]:
        """Get modules available for required capabilities.

        Returns dict mapping capability -> list of worker IDs that can fulfill it.
        """
        result: Dict[str, List[str]] = {}
        for cap in required_capabilities:
            result[cap] = self._cached_capabilities.get(cap, [])
        return result

    def serialize_for_transport(self) -> dict:
        """Serialize cached capabilities for network transport."""
        return {
            "capabilities": {
                cap: list(workers)
                for cap, workers in self._cached_capabilities.items()
            }
        }

    @classmethod
    def deserialize_from_transport(cls, data: dict) -> "ModuleDiscovery":
        """Deserialize capabilities from network transport."""
        discovery = cls()
        if "capabilities" in data:
            for cap, workers in data["capabilities"].items():
                discovery._cached_capabilities[cap] = workers
        return discovery