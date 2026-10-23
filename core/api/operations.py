"""System API operations routes (Issue #110 Phase E).

Extracted verbatim from ``core/app.py``: the same paths, methods and response
codes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..operations import get_operation, list_operations

router = APIRouter()


@router.get("/api/operations")
async def list_operations_api(limit: int = 50):
    return list_operations(limit)


@router.get("/api/operations/{operation_id}")
async def get_operation_api(operation_id: str):
    operation = get_operation(operation_id)
    if not operation:
        raise HTTPException(404, "Operation not found")
    return operation
