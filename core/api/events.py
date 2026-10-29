from __future__ import annotations

from core.state import store
from fastapi.responses import StreamingResponse
import asyncio
import json
from fastapi import APIRouter
router = APIRouter()

@router.get("/api/events")
async def event_stream():
    async def generate():
        snapshots: dict[str, int] = {}
        while True:
            changes = []
            for job in store.jobs.values():
                count = len(job.events)
                if snapshots.get(job.job_id, 0) < count:
                    changes.append({"job_id": job.job_id, "status": job.status.value,
                                    "event": job.events[-1] if job.events else ""})
                    snapshots[job.job_id] = count
            yield ("data: " + json.dumps(changes, ensure_ascii=False) + "\n\n") if changes else ": keepalive\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(generate(), media_type="text/event-stream")

