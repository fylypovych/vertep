"""Stateless worker-selection service separated from Core."""

from fastapi import FastAPI
from pydantic import BaseModel, Field

from core.dispatcher import available_worker
from core.models import Job


app = FastAPI(title="Vertep Dispatcher", version="1")


class DispatchRequest(BaseModel):
    job: dict
    workers: list[dict] = Field(default_factory=list)


@app.get("/health")
def health() -> dict:
    return {"status": "HEALTHY", "service": "dispatcher"}


@app.post("/select")
def select_worker(request: DispatchRequest) -> dict:
    job = Job.model_validate(request.job)
    selected = available_worker(request.workers, job)
    return {"worker": selected}
