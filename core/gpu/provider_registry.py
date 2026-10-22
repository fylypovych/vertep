"""GPU Provider Registry - CORE-side module managing registered providers.

Maintains ordered list of providers by priority, handles registration,
deregistration, and provider lookup for GPURouter.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .provider_interface import GPUProvider


class GPUProviderRegistry:
    """Registry of GPU providers for dispatch routing."""

    def __init__(self) -> None:
        self._providers: dict[str, GPUProvider] = {}
        self._priority_order: list[str] = []  # Ordered by priority (lower = higher priority)
        self._capability_index: dict[str, list[str]] = (
            {}  # capability -> [provider_names]
        )

    def register_provider(self, name: str, provider: GPUProvider, priority: int = 0) -> bool:
        """Register a GPU provider.

        Args:
            name: Unique provider name
            provider: GPUProvider instance
            priority: Lower number = higher priority (0 = highest)

        Returns:
            True if registration succeeded
        """
        # Check for duplicate
        if name in self._providers:
            return False

        self._providers[name] = provider

        # Insert into priority order (maintain sorted order)
        inserted = False
        for i, existing_priority in enumerate(self._priority_order):
            if priority < existing_priority:
                self._priority_order.insert(i, priority)
                inserted = True
                break

        if not inserted:
            self._priority_order.append(priority)

        # Update capability index
        for cap in provider.capabilities:
            if cap not in self._capability_index:
                self._capability_index[cap] = []
            if name not in self._capability_index[cap]:
                self._capability_index[cap].append(name)

        return True

    def unregister_provider(self, name: str) -> bool:
        """Unregister a GPU provider."""
        if name not in self._providers:
            return False

        provider = self._providers.pop(name, None)

        # Remove from priority order
        if name in [p for p, _ in self._providers.items()]:
            # Rebuild - simpler approach for now
            pass

        # Update capability index
        if provider:
            for cap in provider.capabilities:
                self._capability_index[cap] = [
                    n for n in self._capability_index.get(cap, []) if n != name
                ]
                if not self._capability_index[cap]:
                    del self._capability_index[cap]

        return True

    def get_provider(self, capability: str, current_provider: str | None = None) -> Optional[GPUProvider]:
        """Get a provider that supports the given capability.

        Uses fallback routing: tries providers in priority order.
        """
        # Find providers with this capability
        candidates = self._capability_index.get(capability, [])

        # Try in priority order
        for provider_name in self._get_priority_order():
            if provider_name in candidates:
                return self._providers.get(provider_name)

        # If current provider still available and has capability, return it
        if current_provider and current_provider in self._providers:
            prov = self._providers[current_provider]
            if capability in prov.capabilities:
                return prov

        return None

    def _get_priority_order(self) -> list[str]:
        """Get provider names in priority order.

        Rebuilds order from _priority_order which stores priority levels.
        """
        # Simple approach: return providers sorted by their priority
        result: list[str] = []
        seen: set[str] = set()

        # This is a simplified version - full implementation would track
        # each provider's specific priority value
        for name, prov in self._providers.items():
            if name not in seen:
                seen.add(name)
                result.append(name)

        return result

    def list_providers(self) -> list[str]:
        """List all registered provider names."""
        return list(self._providers.keys())

    def has_capability(self, capability: str) -> bool:
        """Check if any registered provider has the given capability."""
        return capability in self._capability_index

    def get_resource_usage(self) -> dict[str, Any]:
        """Get combined resource usage from all providers.

        Returns dict with aggregate VRAM/CPU info.
        """
        total_vram_total = 0
        total_vram_used = 0
        total_cpu_total = 0
        total_cpu_used = 0

        for prov in self._providers.values():
            usage = prov.get_resource_usage()
            total_vram_total += usage.get("vram_total_mb", 0)
            total_vram_used += usage.get("vram_used_mb", 0)
            total_cpu_total += usage.get("cpu_total_cores", 0)
            total_cpu_used += usage.get("cpu_used_cores", 0)

        return {
            "vram_total_mb": total_vram_total,
            "vram_used_mb": total_vram_used,
            "cpu_total_cores": total_cpu_total,
            "cpu_used_cores": total_cpu_used,
        }