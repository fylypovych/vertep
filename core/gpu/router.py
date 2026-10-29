"""GPU Router - fallback routing and provider selection.

Implements the GPU Provider contract from Architecture 2.0.
Handles provider fallback routing, readiness checks, and resource management.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
from threading import RLock

from .provider_interface import GPUProvider
from .provider_registry import GPUProviderRegistry


class GPURouter:
    """Router for GPU provider fallback and task routing."""

    def __init__(self, registry: GPUProviderRegistry | None = None, allowed: list[str] | None = None, local_only: bool = False) -> None:
        self.registry = registry or GPUProviderRegistry()
        self._fallback_attempts: dict[str, int] = {}  # task_id -> fallback count
        self._fallback_cooldown = timedelta(seconds=30)
        self.allowed = set(allowed if allowed is not None else self.registry.list_providers())
        self.local_only = local_only
        self._active = {}
        self._reservations = {}
        self._operations = {}
        self._operation_ids = {}
        self._lock = RLock()
        self.failures = {}

    def execute(self, capability: str, task: dict) -> dict:
        """Submit only to configured candidates, retaining uncertain reservations."""
        task_id = task["task_id"]
        operation_id = task.get("inference_operation_id", task_id)
        reasons = []
        with self._lock:
            if task_id in self._active or operation_id in self._operations:
                raise RuntimeError("Inference already submitted")
            for name in self.registry._get_priority_order():
                provider = self.registry._providers[name]
                if name not in self.allowed or (self.local_only and not getattr(provider, "is_local", False)):
                    continue
                if capability not in provider.capabilities:
                    continue
                try:
                    ready = provider.query_capability({"type": capability, **task})
                    if not ready.get("available"):
                        reasons.append(f"{name}: not ready")
                        continue
                    if sum(owner == name for owner in self._active.values()) >= provider.max_concurrent_tasks:
                        reasons.append(f"{name}: capacity exhausted")
                        continue
                    usage = provider.get_resource_usage()
                    required = task.get("vram_mb", provider.min_vram_mb)
                    if not isinstance(required, int) or isinstance(required, bool) or required < 0:
                        raise ValueError("Invalid VRAM requirement")
                    reserved = sum(amount for task_key, amount in self._reservations.items() if self._active.get(task_key) == name)
                    if usage.get("vram_total_mb", 0) - usage.get("vram_used_mb", 0) - reserved < required:
                        reasons.append(f"{name}: insufficient VRAM")
                        continue
                except Exception as exc:
                    reasons.append(f"{name}: readiness {type(exc).__name__}")
                    continue
                self._active[task_id] = name
                self._reservations[task_id] = required
                self._operations[operation_id] = task_id
                self._operation_ids[task_id] = operation_id
                try:
                    self._lock.release()
                    try:
                        result = provider.submit_inference({**task, "type": capability})
                    finally:
                        self._lock.acquire()
                except Exception as exc:
                    # Submission may have reached the provider. Never infer that
                    # a transport timeout means inference has stopped.
                    self.failures[task_id] = [*reasons, f"{name}: uncertain submission {type(exc).__name__}"]
                    raise RuntimeError("Inference submission uncertain; reconcile before retry") from exc
                if self._active.get(task_id) != name:
                    raise RuntimeError("Cancelled inference result rejected")
                if result.get("status") in {"complete", "completed"}:
                    self._release(task_id)
                    return result
                if result.get("status") == "failed" and result.get("cleanup") is True:
                    self._release(task_id)
                    reasons.append(f"{name}: failed with cleanup confirmed")
                    continue
                return result
        self.failures[task_id] = reasons
        raise RuntimeError("Allowed GPU providers exhausted: " + "; ".join(reasons))

    def _release(self, task_id):
        self._active.pop(task_id, None)
        self._reservations.pop(task_id, None)
        operation = self._operation_ids.pop(task_id, None)
        if operation is not None:
            self._operations.pop(operation, None)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            name = self._active.get(task_id)
            if name is None:
                return False
            result = self.registry._providers[name].cancel_inference(task_id)
            if result.get("cancelled") and result.get("cleanup"):
                self._release(task_id)
                return True
            return False

    def route_inference(
        self, capability: str, task: dict[str, Any], current_provider: str | None = None
    ) -> Optional[GPUProvider]:
        """Route an inference task to the best available provider.

        Tries providers in priority order, with fallback on failure.
        """
        # Try providers in priority order
        for provider_name in self.registry._get_priority_order():
            if provider_name not in self.allowed:
                continue
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
            if current_provider in self.allowed and prov and capability in prov.capabilities and self._is_provider_ready(prov):
                return prov

        # Fall through to priority-based routing
        return self.route_inference(capability, task, current_provider)

    def _is_provider_ready(self, provider: GPUProvider) -> bool:
        """Check if a provider is ready to accept tasks.

        In a full implementation, this would check health, VRAM availability,
        etc. For now, assumes ready if provider is registered.
        """
        if self.local_only and not getattr(provider, "is_local", False):
            return False
        try:
            return any(provider.query_capability({"type": capability}).get("available", False) for capability in provider.capabilities)
        except Exception:
            return False

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
            if provider_name not in self.allowed:
                continue
            prov = self.registry._providers.get(provider_name)
            if prov is None:
                continue

            if prov.name in exclude:
                continue

            if capability in prov.capabilities and self._is_provider_ready(prov):
                return prov

        return None
