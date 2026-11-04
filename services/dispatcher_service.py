"""Stateless worker-selection service separated from Core."""

from fastapi import FastAPI
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


@app.get("/health")
def health() -> dict:
    return {"status": "HEALTHY", "service": "dispatcher"}


@app.post("/select")
def select_worker(request: DispatchRequest) -> dict:
    job = Job.model_validate(request.job)
    selected = available_worker(request.workers, job, task_type=request.task_type,
                                min_vram_mb=request.min_vram_mb,
                                voice_requirements=request.voice_requirements)
    return {"worker": selected}
