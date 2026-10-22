"""GPU Router - fallback routing and provider selection.

Implements the GPU Provider contract from Architecture 2.0.
Handles provider fallback routing, readiness checks, and resource management.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .provider_interface import GPUProvider
from .provider_registry import GPUProviderRegistry


class GPURouter:
    """Router for GPU provider fallback and task routing."""

    def __init__(self, registry: GPUProviderRegistry | None = None) -> None:
        self.registry = registry or GPUProviderRegistry()
        self._fallback_attempts: dict[str, int] = {}  # task_id -> fallback count
        self._fallback_cooldown = timedelta(seconds=30)

    def route_inference(
        self, capability: str, task: dict[str, Any], current_provider: str | None = None
    ) -> Optional[GPUProvider]:
        """Route an inference task to the best available provider.

        Tries providers in priority order, with fallback on failure.
        """
        # Try providers in priority order
        for provider_name in self.registry._get_priority_order():
            prov = self.registry._providers.get(provider_name)
            if prov is None:
                continue

            # Check capability
            if capability not in prov.capabilities:
                continue

            # Check if provider is ready (basic check)
            if not self._is_provider_ready(prov):
                continue

            return prov

        # No provider available
        return None

    def route_with_fallback(
        self, capability: str, task: dict[str, Any], current_provider: str | None = None
    ) -> Optional[GPUProvider]:
        """Route with explicit fallback logic.

        Tries current provider first, then falls back through priority list.
        """
        # Try current provider first if specified
        if current_provider:
            prov = self.registry._providers.get(current_provider)
            if prov and capability in prov.capabilities and self._is_provider_ready(prov):
                return prov

        # Fall through to priority-based routing
        return self.route_inference(capability, task, current_provider)

    def _is_provider_ready(self, provider: GPUProvider) -> bool:
        """Check if a provider is ready to accept tasks.

        In a full implementation, this would check health, VRAM availability,
        etc. For now, assumes ready if provider is registered.
        """
        # Basic check - provider exists and has the capability
        return True

    def handle_provider_failure(
        self, provider_name: str, task_id: str, reason: str
    ) -> None:
        """Handle provider failure and trigger fallback.

        Logs the failure and records the attempt for cooldown tracking.
        """
        # Record the failure
        self._fallback_attempts[task_id] = self._fallback_attempts.get(task_id, 0) + 1

        # In a full implementation, would:
        # 1. Log the failure with reason
        # 2. Trigger fallback to next provider
        # 3. Apply cooldown before retrying same provider
        pass

    def get_best_provider(
        self, capability: str, exclude: set[str] | None = None
    ) -> Optional[GPUProvider]:
        """Get the best available provider for a capability.

        Excludes any providers in the exclude set (e.g., recently failed ones).
        """
        exclude = exclude or set()

        for provider_name in self.registry._get_priority_order():
            prov = self.registry._providers.get(provider_name)
            if prov is None:
                continue

            if prov.name in exclude:
                continue

            if capability in prov.capabilities and self._is_provider_ready(prov):
                return prov

        return None