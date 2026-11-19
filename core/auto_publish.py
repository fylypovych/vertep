"""Automatic publication (#502 AC3, owner decision 19.11.2026).

A channel publishes automatically when its effective policy is ``automatic``:
the channel's own override, or the brand's policy when the channel inherits it.
It runs when the material becomes publishable (video approved, or the last
foreign owner approved shared content) through the ordinary publish path, so
every guard applies: owners' approvals, the hash at the send boundary,
revocation, and channel credentials. A blocked attempt is recorded, never forced.
"""

from __future__ import annotations


def effective_policy(brand: dict | None, channel) -> str:
    override = (channel.metadata or {}).get("policy", "inherit") if channel is not None else "inherit"
    if override in {"automatic", "approval_required"}:
        return override
    return (brand or {}).get("publication_policy") or "approval_required"


def automatic_channels(brand_id: str) -> list[str]:
    from .brand_store import BrandStoreError, get_brand, list_channels
    from .module.gateway import call_module
    try:
        brand = get_brand(brand_id)
        channels = [channel for channel in list_channels(brand_id) if channel.enabled]
    except BrandStoreError:
        return []
    result = []
    for channel in channels:
        policy = None
        try:
            answer = call_module("brands", "GET", f"/brands/{brand_id}/channels/{channel.channel_id}/policy")
        except Exception:
            answer = None
        if answer and answer[0] == 200 and isinstance(answer[1], dict):
            policy = answer[1].get("policy")
        if (policy or effective_policy(brand, channel)) == "automatic" and channel.channel_type not in result:
            result.append(channel.channel_type)
    return result


def publish_automatically(job_id: str) -> dict:
    """Publish the job to its automatic channels if it is publishable now."""
    from fastapi import HTTPException
    from .models import JobStatus
    from .state import store
    job = store.jobs.get(job_id)
    if job is None or job.status != JobStatus.READY or job.publish_task_ids:
        return {"published": [], "reason": "not ready"}
    channels = automatic_channels(job.brand_id)
    if not channels:
        return {"published": [], "reason": "no automatic channels"}
    from .api.jobs import publish_job
    try:
        publish_job(job_id, channels)
    except HTTPException as error:
        store.event(job, f"AUTOMATIC PUBLICATION WAITING: {error.detail}")
        return {"published": [], "reason": str(error.detail)}
    store.event(job, f"AUTOMATIC PUBLICATION STARTED: {', '.join(channels)}")
    return {"published": channels, "reason": ""}
