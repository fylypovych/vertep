"""Machine codes of API errors: the panel shows their text in the chosen language (#435).

``detail`` stays as it was, because Worker, CLI and Telegram read it. A message built at run
time has no fixed code and is reported by its HTTP status.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

ERROR_CODES: dict[str, str] = json.loads(
    (Path(__file__).with_name("api_error_codes.json")).read_text(encoding="utf-8"))


def error_code(detail: object, status: int) -> str:
    return ERROR_CODES.get(detail, f"http_{status}") if isinstance(detail, str) else f"http_{status}"


def install_error_codes(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def with_code(request: Request, error: HTTPException):
        response = await http_exception_handler(request, error)
        if not isinstance(response, JSONResponse):
            return response  # a status without a body
        return JSONResponse({"detail": error.detail, "code": error_code(error.detail, error.status_code)},
                            status_code=error.status_code, headers=getattr(error, "headers", None))
