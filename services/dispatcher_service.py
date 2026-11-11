"""Stateless worker-selection service separated from Core."""

import hmac
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from core.dispatcher import available_worker
from core.models import Job


app = FastAPI(title="Vertep Dispatcher", version="1")


class DispatchRequest(BaseModel):
    job: dict
    workers: list[dict] = Field(default_factory=list)
    task_type: str | None = None
    min_vram_mb: int | None = None
    voice_requirements: dict | None = None


def require_internal_key(x_vertep_internal_key: str = Header(default="")) -> None:
    """Only CORE (holding INTERNAL_API_KEY) may call the policy endpoint."""
    expected = os.getenv("INTERNAL_API_KEY", "")
    if not expected or not hmac.compare_digest(x_vertep_internal_key, expected):
        raise HTTPException(401, "Internal key required")


@app.get("/health")
def health() -> dict:
    return {"status": "HEALTHY", "service": "dispatcher"}


@app.post("/select", dependencies=[Depends(require_internal_key)])
def select_worker(request: DispatchRequest) -> dict:
    job = Job.model_validate(request.job)
    selected = available_worker(request.workers, job, task_type=request.task_type,
                                min_vram_mb=request.min_vram_mb,
                                voice_requirements=request.voice_requirements)
    return {"worker": selected}
