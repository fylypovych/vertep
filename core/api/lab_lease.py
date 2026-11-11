"""Production side of the laboratory GPU idle-lease contract (docs/lab/lab-gpu-contract.md).

Off unless the owner sets LAB_IDLE_SHARING_ENABLED=true, LAB_LEASE_TOKEN and
LAB_GPU_ENDPOINT. Production always wins: a lease is granted only while no task is
queued or running, the next heartbeat after work appears revokes it, and GPU workers
do not claim until the laboratory confirms the release or the grace period ends.

The laboratory reaches the model only through /inference while it holds a lease: no
GPU service port is published, and a revoke cancels the running call. This is a gated
forwarder to LAB_GPU_ENDPOINT; CORE itself executes no generation.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..state import store, task_queue
from ..system_state import dispatch_allowed

CONTRACT_VERSION = "1"
GPU_CAPABILITIES = {"image_generation", "video_generation", "text_generation"}

router = APIRouter(prefix="/lab-lease/v1")
# ponytail: in-memory, one CORE; after a CORE restart the lease is unknown, so the next
# heartbeat stops the laboratory. Use shared storage if CORE ever runs as replicas.
_leases: dict[str, dict] = {}
_lock = threading.RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enabled() -> bool:
    return (os.getenv("LAB_IDLE_SHARING_ENABLED", "false").lower() == "true"
            and bool(os.getenv("LAB_LEASE_TOKEN")))


def _grace_seconds() -> int:
    return int(os.getenv("LAB_LEASE_RELEASE_GRACE_SECONDS", "90"))


def _authorize(request: Request) -> None:
    token = os.getenv("LAB_LEASE_TOKEN", "")
    if not token or not secrets.compare_digest(
            request.headers.get("x-vertep-lab-token", ""), token):
        raise HTTPException(401, "Invalid laboratory token")


def _is_gpu_worker(worker: dict) -> bool:
    capabilities = set(worker.get("capabilities") or [])
    return bool(capabilities & GPU_CAPABILITIES) or int(worker.get("vram_mb") or 0) > 0


def _free_vram_mb() -> int:
    values = []
    for worker in store.workers.values():
        if _is_gpu_worker(worker):
            free = worker.get("free_vram_mb")
            values.append(int(worker.get("vram_mb") or 0) if free is None else int(free))
    return max(values, default=0)


def _production_pressure() -> tuple[int, bool]:
    busy = task_queue.inflight_depth() > 0 or any(
        worker.get("current_task") for worker in store.workers.values())
    return task_queue.depth(), busy


def _drop_finished(now: datetime) -> None:
    for lease_id, lease in list(_leases.items()):
        deadline = lease.get("reclaim_at") or lease["expires_at"]
        if now >= datetime.fromisoformat(deadline):
            _leases.pop(lease_id, None)


def _revoke_all(now: datetime) -> None:
    for lease in _leases.values():
        if lease["state"] == "GRANTED":
            lease["state"] = "REVOKING"
            lease["reclaim_at"] = (now + timedelta(seconds=_grace_seconds())).isoformat()


def hold_gpu_for_laboratory(worker: dict) -> bool:
    """Called on claim: revoke for queued work and hold GPU workers until released."""
    if not _leases or not _is_gpu_worker(worker):
        return False
    now = _now()
    with _lock:
        _drop_finished(now)
        if task_queue.depth() > 0:
            _revoke_all(now)
            return bool(_leases)
    return False


def _endpoint() -> str:
    return os.getenv("LAB_GPU_ENDPOINT", "").strip().rstrip("/")


async def _free_gpu_for_laboratory() -> None:
    """Unload production image models before the grant; a failure denies the lease."""
    url = os.getenv("LAB_GPU_FREE_URL", "").strip()
    if url:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json={"unload_models": True, "free_memory": True})
            response.raise_for_status()


async def _unload_model(lease: dict) -> None:
    """Ask Ollama to drop the model from VRAM before production takes the GPU back."""
    if not lease.get("model") or not _endpoint():
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(f"{_endpoint()}/api/generate",
                              json={"model": lease["model"].removeprefix("ollama:"), "keep_alive": 0})
    except httpx.HTTPError:
        pass  # Released anyway; the real-GPU qualification (#329) measures VRAM.


@router.get("/status")
def status(request: Request) -> dict:
    _authorize(request)
    queue_depth, busy = _production_pressure()
    allowed = dispatch_allowed()
    with _lock:
        _drop_finished(_now())
        active = sorted(_leases)
    free = _free_vram_mb()
    return {"contract_version": CONTRACT_VERSION, "provider": "vertep",
            "gpu_available": bool(_enabled() and allowed and not queue_depth and not busy
                                  and free > 0 and not active),
            "free_vram_mb": free, "queue_depth": queue_depth, "reserved": not allowed,
            "gpu_busy": busy, "active_leases": active, "reason": "",
            "idle_sharing_enabled": _enabled()}


@router.post("/lease/request")
async def request_lease(request: Request) -> dict:
    _authorize(request)
    body = await request.json()
    requirement = body.get("requirement") or {}

    def deny(reason: str) -> dict:
        return {"contract_version": CONTRACT_VERSION, "granted": False, "lease": None, "reason": reason}

    if not _enabled() or not _endpoint():
        return deny("idle sharing is disabled by policy")
    try:
        await _free_gpu_for_laboratory()
    except httpx.HTTPError:
        return deny("GPU could not be freed for the laboratory")
    now = _now()
    with _lock:
        _drop_finished(now)
        queue_depth, busy = _production_pressure()
        if not dispatch_allowed():
            return deny("GPU is reserved for production")
        if queue_depth:
            return deny("production queue is not empty")
        if busy:
            return deny("GPU is busy with production work")
        if _leases:
            return deny("another laboratory lease is active")
        vram_mb = int(requirement.get("vram_mb") or 0)
        free = _free_vram_mb()
        if vram_mb > free:
            return deny(f"model needs {vram_mb} MB, only {free} MB free")
        ttl = max(1, int(body.get("ttl_seconds") or 900))
        lease = {"lease_id": f"lease-{uuid.uuid4().hex}", "endpoint": "/lab-lease/v1/inference",
                 "model": str(requirement.get("model") or ""), "granted_at": now.isoformat(),
                 "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
                 "heartbeat_seconds": 30, "state": "GRANTED", "vram_mb": vram_mb}
        _leases[lease["lease_id"]] = lease
    return {"contract_version": CONTRACT_VERSION, "granted": True, "lease": dict(lease), "reason": ""}


@router.post("/lease/heartbeat")
async def heartbeat(request: Request) -> dict:
    _authorize(request)
    body = await request.json()
    now = _now()

    def answer(active: bool, revoked: bool, expires_at: str, reason: str) -> dict:
        return {"contract_version": CONTRACT_VERSION, "active": active, "revoked": revoked,
                "expires_at": expires_at, "reason": reason}

    with _lock:
        lease = _leases.get(str(body.get("lease_id") or ""))
        if lease is None:
            return answer(False, False, "", "unknown lease")
        queue_depth, busy = _production_pressure()
        if lease["state"] == "GRANTED" and (queue_depth or busy or not dispatch_allowed()):
            _revoke_all(now)
        if lease["state"] != "GRANTED":
            return answer(False, True, lease["expires_at"], "production work arrived: lease revoked")
        if now >= datetime.fromisoformat(lease["expires_at"]):
            _leases.pop(lease["lease_id"], None)
            return answer(False, False, lease["expires_at"], "lease expired")
        ttl = max(1, int(body.get("ttl_seconds") or 900))
        lease["expires_at"] = (now + timedelta(seconds=ttl)).isoformat()
        return answer(True, False, lease["expires_at"], "")


@router.post("/lease/release")
async def release(request: Request) -> dict:
    _authorize(request)
    body = await request.json()
    lease_id = str(body.get("lease_id") or "")
    with _lock:
        lease = _leases.get(lease_id)
    if lease is not None:
        # The entry keeps holding GPU workers until the model has left VRAM.
        await _unload_model(lease)
    with _lock:
        removed = _leases.pop(lease_id, None)
    if removed is None:
        return {"contract_version": CONTRACT_VERSION, "released": False, "reason": "unknown lease"}
    return {"contract_version": CONTRACT_VERSION, "released": True, "reason": "",
            "free_vram_mb": _free_vram_mb()}


@router.post("/inference/api/{operation}")
async def inference(operation: str, request: Request) -> dict:
    """Forward one non-streaming call, only while the named lease stays granted."""
    _authorize(request)
    if operation not in {"generate", "chat"}:
        raise HTTPException(404, "Unsupported inference operation")
    lease_id = request.headers.get("x-vertep-lab-lease", "")

    def usable() -> bool:
        with _lock:
            lease = _leases.get(lease_id)
            return bool(lease and lease["state"] == "GRANTED"
                        and _now() < datetime.fromisoformat(lease["expires_at"]))

    if not usable():
        raise HTTPException(409, "No active laboratory lease")
    body = {**await request.json(), "stream": False}
    timeout = float(os.getenv("LAB_INFERENCE_TIMEOUT_SECONDS", "600"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        call = asyncio.create_task(client.post(f"{_endpoint()}/api/{operation}", json=body))
        while not call.done():
            await asyncio.wait({call}, timeout=0.2)
            if not call.done():
                queue_depth, _ = _production_pressure()
                if queue_depth:
                    with _lock:
                        _revoke_all(_now())
                if not usable():
                    # Closing the connection makes Ollama abort the generation.
                    call.cancel()
                    raise HTTPException(409, "Laboratory lease revoked")
        try:
            response = call.result()
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(502, "GPU endpoint failed") from error
