"""Workflow CRUD routes for the Vertep CORE web application."""
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..configuration import read_json
from ..state import store, workflow_registry

router = APIRouter()


@router.get("/api/workflows")
def workflows():
    return workflow_registry.list()


@router.get("/api/workflows/{kind}/{name}")
def get_workflow(kind: str, name: str):
    try:
        return workflow_registry.load(kind, name)
    except (ValueError, OSError) as error:
        raise HTTPException(404, str(error)) from error


@router.put("/api/workflows/{kind}/{name}")
def put_workflow(kind: str, name: str, workflow: dict):
    try:
        return workflow_registry.save(kind, name, workflow)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/api/workflows/{kind}/{name}")
def delete_workflow(kind: str, name: str):
    reference = f"workflows/{kind}/{name}"
    if any(job.workflow == reference for job in store.jobs.values()):
        raise HTTPException(409, "Сценарій використовується у завданнях")
    character_root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    for directory in character_root.iterdir() if character_root.is_dir() else []:
        if (read_json(directory / "character.json").get("workflow") == reference
                or read_json(directory / "generation.json").get("workflow") == reference):
            raise HTTPException(409, f"Сценарій використовує персонаж {directory.name}")
    try:
        return workflow_registry.delete(kind, name)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(404, "Workflow not found") from error
