"""The narrow, versioned GPU idle-lease contract between lab and Vertep.

Issue #107 (var.0.0.1.7) requires exactly one integration boundary:

    request → grant/deny → lease → heartbeat/renew → revoke → release confirmed

with ``status``/``health`` as separate, read-only operations.  The laboratory
never imports production CORE or Dispatcher internals and never touches their
Redis, PostgreSQL, Docker socket or volumes: it talks to an authorised, narrow
endpoint through :class:`VertepResourceClient`, and to
:class:`FakeResourceProvider` in tests.

**Vertep has absolute priority.**  A lease is only handed out when production
has no queue pressure and no reservation; when production work appears, new
requests are refused, the current inference is stopped at a controlled point,
VRAM is released and the release is confirmed.  Instant preemption is never
promised — if a safe release cannot be guaranteed, no lease is granted.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from .state_store import read_json, write_json


CONTRACT_VERSION = "1"

#: Header carrying the lab's minimal-permission credential.  The value is never
#: logged; the gate audits only the decision, not the secret.
TOKEN_HEADER = "X-Vertep-Lab-Token"


class LeaseState(str, Enum):
    IDLE = "IDLE"
    REQUESTED = "REQUESTED"
    GRANTED = "GRANTED"
    REVOKING = "REVOKING"
    RELEASED = "RELEASED"
    DENIED = "DENIED"


class ResourceUnavailable(RuntimeError):
    """The production resource endpoint could not be reached or answered badly."""


class LeaseDenied(RuntimeError):
    """Production refused the lease (queue pressure, reservation or busy GPU)."""


class LeaseLost(RuntimeError):
    """The lease was revoked or expired — the lab must stop using the GPU now."""


@dataclass(frozen=True)
class ResourceRequirement:
    """What the lab asks for; production decides whether it may have it."""

    model: str
    vram_mb: int = 0
    kind: str = "llm_inference"
    max_seconds: int = 900

    def as_dict(self) -> dict:
        return {"model": self.model, "vram_mb": self.vram_mb, "kind": self.kind,
                "max_seconds": self.max_seconds}


@dataclass
class Lease:
    """A granted, expiring permission to use an inference endpoint."""

    lease_id: str
    endpoint: str
    model: str
    granted_at: str
    expires_at: str
    heartbeat_seconds: int = 60
    state: LeaseState = LeaseState.GRANTED
    vram_mb: int = 0

    @property
    def expires(self) -> datetime:
        value = datetime.fromisoformat(self.expires_at)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def active(self, now: datetime | None = None) -> bool:
        return (self.state == LeaseState.GRANTED
                and (now or datetime.now(timezone.utc)) < self.expires)

    def as_dict(self) -> dict:
        return {"lease_id": self.lease_id, "endpoint": self.endpoint, "model": self.model,
                "granted_at": self.granted_at, "expires_at": self.expires_at,
                "heartbeat_seconds": self.heartbeat_seconds, "state": self.state.value,
                "vram_mb": self.vram_mb}


class ResourceProvider:
    """Interface the laboratory depends on; production supplies the adapter."""

    name = "abstract"

    def status(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def request(self, requirement: ResourceRequirement, ttl_seconds: int) -> dict:
        """Return ``{"granted": bool, "lease": dict|None, "reason": str}``."""
        raise NotImplementedError

    def heartbeat(self, lease_id: str, ttl_seconds: int) -> dict:
        """Return ``{"active": bool, "revoked": bool, "expires_at": str}``."""
        raise NotImplementedError

    def release(self, lease_id: str) -> dict:
        """Confirm the resource is free again for production."""
        raise NotImplementedError


class FakeResourceProvider(ResourceProvider):
    """Deterministic production stand-in used by every contract test.

    It reproduces the properties the real dispatcher must guarantee: production
    priority, denial under queue pressure or reservation, lease expiry, revoke
    (including mid-inference), connection loss and restart recovery.
    """

    name = "fake"

    def __init__(self, *, vram_mb: int = 24576, queue_depth: int = 0, reserved: bool = False,
                 gpu_busy: bool = False, offline: bool = False,
                 revoke_after_heartbeats: int = 0, endpoint: str = "http://gpu.local:11434"):
        self.vram_mb = vram_mb
        self.queue_depth = queue_depth
        self.reserved = reserved
        self.gpu_busy = gpu_busy
        self.offline = offline
        self.revoke_after_heartbeats = revoke_after_heartbeats
        self.endpoint = endpoint
        self.leases: dict[str, dict] = {}
        self.released: list[str] = []
        self.requests = 0
        self._heartbeats = 0
        self._revoked: set[str] = set()

    # -- helpers ---------------------------------------------------------
    def _check_link(self) -> None:
        if self.offline:
            raise ResourceUnavailable("production resource endpoint unreachable")

    def _lease_available(self) -> tuple[bool, str]:
        if self.queue_depth > 0:
            return False, "production queue is not empty"
        if self.reserved:
            return False, "GPU is reserved for production"
        if self.gpu_busy:
            return False, "GPU is busy with production work"
        return True, ""

    # -- contract --------------------------------------------------------
    def status(self) -> dict:
        self._check_link()
        available, reason = self._lease_available()
        idle_sharing = os.getenv("LAB_IDLE_SHARING_ENABLED", "false").lower() == "true"
        return {"contract_version": CONTRACT_VERSION, "provider": self.name,
                "gpu_available": bool(available and self.vram_mb > 0),
                "free_vram_mb": self.vram_mb, "queue_depth": self.queue_depth,
                "reserved": self.reserved, "gpu_busy": self.gpu_busy,
                "active_leases": sorted(self.leases), "reason": reason,
                "idle_sharing_enabled": idle_sharing}

    def request(self, requirement: ResourceRequirement, ttl_seconds: int) -> dict:
        self._check_link()
        self.requests += 1
        available, reason = self._lease_available()
        if not available:
            return {"granted": False, "lease": None, "reason": reason}
        if requirement.vram_mb > self.vram_mb:
            return {"granted": False, "lease": None,
                    "reason": f"model needs {requirement.vram_mb} MB, "
                              f"only {self.vram_mb} MB free"}
        now = datetime.now(timezone.utc)
        lease_id = f"lease-{len(self.leases) + 1:04d}"
        record = {"lease_id": lease_id, "endpoint": self.endpoint,
                  "model": requirement.model, "granted_at": now.isoformat(),
                  "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
                  "heartbeat_seconds": 30, "state": LeaseState.GRANTED.value,
                  "vram_mb": requirement.vram_mb}
        self.leases[lease_id] = record
        return {"granted": True, "lease": record, "reason": ""}

    def heartbeat(self, lease_id: str, ttl_seconds: int) -> dict:
        self._check_link()
        record = self.leases.get(lease_id)
        if record is None:
            return {"active": False, "revoked": False, "expires_at": "",
                    "reason": "unknown lease"}
        if lease_id in self._revoked:
            record["state"] = LeaseState.RELEASED.value
            return {"active": False, "revoked": True, "expires_at": record["expires_at"],
                    "reason": "production work arrived: lease revoked"}
        if datetime.now(timezone.utc) >= datetime.fromisoformat(record["expires_at"]):
            return {"active": False, "revoked": False, "expires_at": record["expires_at"],
                    "reason": "lease expired"}
        self._heartbeats += 1
        if self.revoke_after_heartbeats and self._heartbeats >= self.revoke_after_heartbeats:
            record["state"] = LeaseState.REVOKING.value
            return {"active": False, "revoked": True, "expires_at": record["expires_at"],
                    "reason": "production work arrived: lease revoked"}
        record["expires_at"] = (datetime.now(timezone.utc)
                                + timedelta(seconds=ttl_seconds)).isoformat()
        return {"active": True, "revoked": False, "expires_at": record["expires_at"],
                "reason": ""}

    def release(self, lease_id: str) -> dict:
        self._check_link()
        record = self.leases.pop(lease_id, None)
        self.released.append(lease_id)
        if record is None:
            return {"released": False, "reason": "unknown lease"}
        record["state"] = LeaseState.RELEASED.value
        self.vram_mb += int(record.get("vram_mb") or 0)
        return {"released": True, "reason": "", "free_vram_mb": self.vram_mb}

    # -- production-side test helpers ------------------------------------
    def simulate_production_work(self, *, queue_depth: int = 1) -> None:
        """Production gets busy: deny new requests and revoke held leases."""
        self.queue_depth = queue_depth
        self._revoked.update(self.leases)

    def force_revoke(self, lease_id: str) -> None:
        self._revoked.add(lease_id)

    def expire(self, lease_id: str) -> None:
        record = self.leases.get(lease_id)
        if record:
            record["expires_at"] = (datetime.now(timezone.utc)
                                    - timedelta(seconds=1)).isoformat()


class VertepResourceClient(ResourceProvider):
    """Production adapter: the only way the lab talks to Vertep resources.

    It speaks the narrow versioned contract over an authorised endpoint and
    nothing else — no production Redis, PostgreSQL, Docker socket or volumes,
    no import of CORE internals.  Replacing this class (for example with a
    module adapter during the var.0.0.1.8 migration) does not change the
    orchestrator or any agent.
    """

    name = "vertep"

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 10.0,
                 enabled: bool | None = None, opener=None):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token if token is not None else os.getenv("LAB_VERTEP_TOKEN", "")
        self.timeout = timeout
        if enabled is None:
            enabled = os.getenv("LAB_IDLE_SHARING_ENABLED", "false").lower() == "true"
        # Fail-closed: idle sharing stays off until the owner enables it and the
        # real-GPU guarantees are proven (Issue #107, criterion 4).
        self.enabled = bool(enabled)
        self._opener = opener or urllib.request.build_opener()

    # -- transport -------------------------------------------------------
    def _call(self, path: str, payload: dict | None = None) -> dict:
        if not self.enabled:
            raise ResourceUnavailable("idle GPU sharing is disabled by policy")
        if not self.base_url:
            raise ResourceUnavailable("no production resource endpoint configured")
        data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data,
                                         method="POST" if data else "GET")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Vertep-Contract-Version", CONTRACT_VERSION)
        if self.token:
            request.add_header(TOKEN_HEADER, self.token)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            raise ResourceUnavailable(
                f"production endpoint refused the request: HTTP {error.code}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ResourceUnavailable(f"production endpoint unreachable: {error}") from error
        if not isinstance(body, dict):
            raise ResourceUnavailable("production endpoint returned a non-object body")
        if str(body.get("contract_version", CONTRACT_VERSION)) != CONTRACT_VERSION:
            raise ResourceUnavailable("unsupported resource contract version")
        return body

    # -- contract --------------------------------------------------------
    def status(self) -> dict:
        return self._call("/status")

    def request(self, requirement: ResourceRequirement, ttl_seconds: int) -> dict:
        return self._call("/lease/request",
                          {"requirement": requirement.as_dict(), "ttl_seconds": ttl_seconds})

    def heartbeat(self, lease_id: str, ttl_seconds: int) -> dict:
        return self._call("/lease/heartbeat",
                          {"lease_id": lease_id, "ttl_seconds": ttl_seconds})

    def release(self, lease_id: str) -> dict:
        return self._call("/lease/release", {"lease_id": lease_id})


class LeaseManager:
    """Lab-side lease lifecycle: acquire → heartbeat → release, fail-closed.

    The manager owns at most one lease.  It refuses to start a new local
    inference while no active lease exists, stops at a controlled point when the
    lease is revoked, and always confirms the release — even after a crash,
    because the lease record is persisted and reconciled on restart.
    """

    def __init__(self, provider: ResourceProvider, *, state_dir=None, actor: str = "lab",
                 ttl_seconds: int = 900, heartbeat_seconds: int = 30):
        self.provider = provider
        self.actor = actor
        self.ttl_seconds = ttl_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.lease: Lease | None = None
        self.stopped_at_control_point = False
        self.state_dir = state_dir
        self._restore()

    # -- persistence -----------------------------------------------------
    @property
    def _state_path(self):
        return None if self.state_dir is None else Path(self.state_dir) / "lease.json"

    def _persist(self) -> None:
        if self._state_path is None:
            return
        write_json(self._state_path, self.lease.as_dict() if self.lease else {})

    def _restore(self) -> None:
        """After a restart the lab must not keep using an unverified lease."""
        if self._state_path is None or not self._state_path.exists():
            return
        record = read_json(self._state_path, default={}) or {}
        if not record or not record.get("lease_id"):
            return
        self.release(record["lease_id"], reason="restart reconciliation")

    # -- lifecycle -------------------------------------------------------
    def status(self) -> dict:
        try:
            return dict(self.provider.status())
        except ResourceUnavailable as error:
            return {"provider": getattr(self.provider, "name", "unknown"),
                    "gpu_available": False, "reason": str(error),
                    "contract_version": CONTRACT_VERSION}

    def acquire(self, requirement: ResourceRequirement) -> Lease:
        """Request a lease; deny raises :class:`LeaseDenied`."""
        if self.lease and self.lease.active():
            raise LeaseDenied("the laboratory already holds a lease")
        try:
            answer = self.provider.request(requirement, self.ttl_seconds)
        except ResourceUnavailable as error:
            raise LeaseDenied(str(error)) from error
        if not answer.get("granted") or not answer.get("lease"):
            raise LeaseDenied(str(answer.get("reason") or "production denied the lease"))
        record = answer["lease"]
        self.lease = Lease(lease_id=record["lease_id"], endpoint=record.get("endpoint", ""),
                           model=record.get("model", requirement.model),
                           granted_at=record.get("granted_at", ""),
                           expires_at=record.get("expires_at", ""),
                           heartbeat_seconds=int(record.get("heartbeat_seconds",
                                                            self.heartbeat_seconds)),
                           vram_mb=int(record.get("vram_mb", requirement.vram_mb)))
        self.stopped_at_control_point = False
        self._persist()
        return self.lease

    def heartbeat(self) -> bool:
        """Renew the lease; a revoke or expiry makes the lab stop immediately."""
        if self.lease is None:
            raise LeaseLost("no lease is held")
        try:
            answer = self.provider.heartbeat(self.lease.lease_id, self.ttl_seconds)
        except ResourceUnavailable as error:
            self.abandon(str(error))
            raise LeaseLost(str(error)) from error
        if not answer.get("active"):
            reason = str(answer.get("reason") or "lease is no longer active")
            self.abandon(reason)
            raise LeaseLost(reason)
        self.lease.expires_at = str(answer.get("expires_at") or self.lease.expires_at)
        self._persist()
        return True

    def abandon(self, reason: str) -> None:
        """Mark the lease lost after a controlled stop; production keeps the GPU."""
        self.stopped_at_control_point = True
        identifier = self.lease.lease_id if self.lease else None
        if self.lease is not None:
            self.lease.state = LeaseState.RELEASED
        self.lease = None
        self._persist()
        self.last_release_reason = reason
        if identifier:
            # The lab stopped using the GPU; confirm the release is idempotent so
            # production can reclaim VRAM immediately.
            try:
                self.provider.release(identifier)
            except ResourceUnavailable:
                pass

    def release(self, lease_id: str | None = None, reason: str = "work finished") -> dict:
        """Confirm the GPU is free again; always attempts the confirmation."""
        identifier = lease_id or (self.lease.lease_id if self.lease else None)
        self.stopped_at_control_point = True
        if self.lease is not None:
            self.lease.state = LeaseState.RELEASED
        self.lease = None
        self._persist()
        self.last_release_reason = reason
        if not identifier:
            return {"released": False, "reason": "no lease to release"}
        try:
            return dict(self.provider.release(identifier))
        except ResourceUnavailable as error:
            # The lab already stopped using the GPU; production reconciles the
            # lease by TTL.  The failure is surfaced, never hidden.
            return {"released": False, "reason": str(error)}

    def assert_usable(self) -> Lease:
        """Guard every local inference: no active lease means no GPU usage."""
        if self.lease is None:
            raise LeaseLost("no active lease: local inference is forbidden")
        if not self.lease.active():
            self.abandon("lease expired")
            raise LeaseLost("lease expired: local inference is forbidden")
        return self.lease

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.lease is not None:
            self.release(reason="context exit")




