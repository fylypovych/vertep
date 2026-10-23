"""System API certificate routes (Issue #110 Phase E).

Extracted verbatim from ``core/app.py``: the same paths, methods and response
codes.  The actual logic calls the Certificate Manager via :func:`_internal_api`.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..api._internal import _internal_api

router = APIRouter()


@router.get("/api/system/certificates")
async def system_certificates():
    return await _internal_api("GET", "CERTIFICATE_MANAGER_URL", "/certificate")


@router.post("/api/system/certificates/renew")
async def renew_system_certificate():
    return await _internal_api("POST", "CERTIFICATE_MANAGER_URL", "/certificate/renew")
