"""Disposable fixtures for the Telegram acceptance (Issue #253).

The acceptance runs the production CORE against a controlled Telegram
transport and a temporary durable state.  The fixtures below create the
objects the acceptance needs — a Job owned by the operator chat, a publish
channel and a Publisher Worker — with the production store API, so the
behaviour under test is the production behaviour.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.models import Channel, JobStatus, VideoVersion, utc_now

OWNER_CHAT = "42"
FOREIGN_CHAT = "99"
CHANNEL_ID = "tg-controlled"
CHANNEL_TYPE = "youtube"
PUBLISHER_NODE = "publisher-1"
JOB_ID = "2026-000001"
VIDEO_BYTES = b"vertep-acceptance-video"


def install(store) -> dict:
    """Create the acceptance Job, channel and Publisher Worker.

    Returns a summary with the identifiers and the artifact digest the
    acceptance has to correlate the publication receipt with.
    """
    job = store.create(
        topic="Telegram acceptance",
        character_id="did_samogon",
        priority=5,
        source=f"telegram:{OWNER_CHAT}",
    )
    video_path = Path(store.root) / job.job_id / "final" / "video.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(VIDEO_BYTES)
    digest = hashlib.sha256(VIDEO_BYTES).hexdigest()

    job.output_path = str(video_path)
    job.video_versions = [VideoVersion(
        version=1,
        path=str(video_path.relative_to(Path(store.root))),
        sha256=digest,
        approved=False,
        created_at=utc_now(),
    )]
    job.active_video_version = 1
    job.approval_status = "pending"
    job.approved = False
    store.update(job, JobStatus.VIDEO_PENDING_APPROVAL, "ACCEPTANCE FIXTURE READY")

    store.repository.save_channel(Channel(
        channel_id=CHANNEL_ID,
        brand_id=job.brand_id,
        channel_type=CHANNEL_TYPE,
        target="@controlled",
        enabled=True,
        created_at=utc_now(),
        metadata={},
    ))

    store.save_worker({
        "node_name": PUBLISHER_NODE,
        "role": "worker",
        "status": "READY",
        "supported_tasks": ["publish"],
        "supported_workflows": ["*"],
        "capabilities": ["publishing"],
        "tested_capabilities": [],
        "self_test": {},
        "cancel_fence_ack": {},
        "model_catalog": {},
        "provider_config": {},
        "current_job": None,
        "current_task": None,
        "last_seen": utc_now(),
    })

    return {
        "job_id": job.job_id,
        "channel_id": CHANNEL_ID,
        "channel_type": CHANNEL_TYPE,
        "publisher_node": PUBLISHER_NODE,
        "video_sha256": digest,
        "video_path": str(video_path),
    }
