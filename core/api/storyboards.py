from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..state import executor, store
from ..storyboard import StoryboardConflict, StoryboardService
from .job_helpers import _prepare_and_dispatch


router = APIRouter()


class StoryboardAction(BaseModel):
    version: int = Field(ge=1)
    actor: str = Field(default="api", min_length=1, max_length=200)


class StoryboardRevision(StoryboardAction):
    revision: str | None = Field(default=None, max_length=4000)


def _service() -> StoryboardService:
    return StoryboardService(store, executor)


def _translate(action):
    try:
        return action()
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except StoryboardConflict as error:
        raise HTTPException(409, str(error)) from error


@router.get("/api/jobs/{job_id}/storyboards")
def list_storyboards(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.storyboards


@router.get("/api/jobs/{job_id}/storyboards/{version}")
def get_storyboard(job_id: str, version: int):
    return _translate(lambda: _service().get(job_id, version))


@router.post("/api/jobs/{job_id}/storyboards/generate")
def generate_storyboard(job_id: str, body: StoryboardRevision | None = None):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _service().queue(job, body.revision if body else None)


@router.post("/api/jobs/{job_id}/storyboards/approve")
def approve_storyboard(job_id: str, body: StoryboardAction):
    job = _translate(lambda: _service().approve(job_id, body.version, body.actor))
    executor.submit(_prepare_and_dispatch, job)
    return job


@router.post("/api/jobs/{job_id}/storyboards/reject")
def reject_storyboard(job_id: str, body: StoryboardAction):
    return _translate(lambda: _service().reject(job_id, body.version, body.actor))


@router.post("/api/jobs/{job_id}/storyboards/regenerate")
def regenerate_storyboard(job_id: str, body: StoryboardRevision):
    return _translate(lambda: _service().regenerate(
        job_id, body.version, body.actor, body.revision
    ))
