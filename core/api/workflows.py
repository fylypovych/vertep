"""Workflow CRUD routes for the Vertep CORE web application."""
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from ..configuration import read_json
from .. import state
from ..workflows import WORKFLOW_TYPES, SAFE_NAME


def workflow_registry():
    return state.workflow_registry

router = APIRouter()


@router.get("/api/workflows")
def workflows():
    return workflow_registry().list()


@router.get("/api/workflows/{kind}/{name}")
def get_workflow(kind: str, name: str):
    try:
        return workflow_registry().load(kind, name)
    except (ValueError, OSError) as error:
        raise HTTPException(404, str(error)) from error


@router.put("/api/workflows/{kind}/{name}")
def put_workflow(kind: str, name: str, workflow: dict, force: bool = Query(False)):
    try:
        return workflow_registry().save(kind, name, workflow, force=force)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/api/workflows/{kind}/{name}")
def delete_workflow(kind: str, name: str, force: bool = Query(False)):
    try:
        return workflow_registry().delete(kind, name, force=force)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(404, "Workflow not found") from error


@router.get("/api/workflows/{kind}/{name}/versions")
def workflow_versions(kind: str, name: str):
    if kind not in WORKFLOW_TYPES or not SAFE_NAME.fullmatch(name):
        raise HTTPException(400, "Invalid workflow type or name")
    try:
        return workflow_registry().get_versions(kind, name)
    except Exception as error:
        raise HTTPException(404, str(error)) from error


@router.get("/api/workflows/{kind}/{name}/versions/{version}")
def get_workflow_version(kind: str, name: str, version: int):
    try:
        return workflow_registry().load_version(kind, name, version)
    except FileNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    except Exception as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/workflows/{kind}/{name}/versions/{version}/restore")
def restore_workflow_version(kind: str, name: str, version: int):
    try:
        return workflow_registry().restore_version(kind, name, version)
    except FileNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/workflows/{kind}/{name}/form")
def workflow_form(kind: str, name: str):
    try:
        return workflow_registry().get_form_schema(kind, name)
    except FileNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    except Exception as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/workflows/{kind}/{name}/usage")
def workflow_usage(kind: str, name: str):
    if kind not in WORKFLOW_TYPES or not SAFE_NAME.fullmatch(name):
        raise HTTPException(400, "Invalid workflow type or name")
    try:
        return workflow_registry().usage(kind, name)
    except FileNotFoundError as error:
        raise HTTPException(404, str(error)) from error
