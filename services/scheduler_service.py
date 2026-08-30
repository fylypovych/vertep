"""Scheduler policy boundary separated from the Core API process."""

from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel, Field


app = FastAPI(title="Vertep Scheduler", version="1")


class ScheduleRequest(BaseModel):
    jobs: list[dict] = Field(default_factory=list)
    limit: int = Field(default=1, ge=1, le=1000)


@app.get("/health")
def health() -> dict:
    return {"status": "HEALTHY", "service": "scheduler"}


@app.post("/due")
def due(request: ScheduleRequest) -> dict:
    now = datetime.now(timezone.utc)
    due_jobs = []
    for item in request.jobs:
        scheduled_for = item.get("scheduled_for")
        if scheduled_for:
            try:
                scheduled = datetime.fromisoformat(str(scheduled_for).replace("Z", "+00:00"))
                if scheduled.tzinfo is None:
                    scheduled = scheduled.replace(tzinfo=timezone.utc)
                if scheduled > now:
                    continue
            except ValueError:
                continue
        due_jobs.append(item)
    ordered = sorted(due_jobs, key=lambda item: (
        -int(item.get("priority", 0)), str(item.get("created_at", "")), str(item.get("job_id", ""))))
    return {"jobs": ordered[:request.limit]}
