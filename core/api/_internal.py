"""Shared internal-API helpers for CORE-to-service HTTP calls.

Extracted from ``core/app.py`` (Issue #110 Phase E).  Both the sync variant
(used by Telegram handlers) and the async variant (used by System API routes)
live here so that no router module needs to reach back into ``core.app``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import HTTPException


def _internal_auth_headers() -> dict[str, str]:
    """Build headers carrying the internal API key for self-calls."""
    key = os.getenv("INTERNAL_API_KEY", "")
    return {"x-vertep-internal-key": key} if key else {}


def _sync_internal_api(
    method: str,
    base_environment: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Synchronous wrapper around an internal service API.

    Returns the parsed JSON body, or ``None`` when the service is not
    configured / unreachable.  Telegram handlers must never raise on a
    missing backend — they degrade to an error message instead.
    """
    base = os.getenv(base_environment, "").rstrip("/")
    if not base:
        return None
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                method, f"{base}{path}", json=payload, headers=headers
            )
            if response.status_code >= 400:
                return {"_error": f"HTTP {response.status_code}"}
            return response.json()
    except (httpx.HTTPError, ValueError):
        return None


_CLIENT_ERROR_STATUSES = {404, 409, 422}


async def _internal_api(
    method: str,
    base_environment: str,
    path: str,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    *,
    propagate_client_errors: bool = False,
) -> dict[str, Any]:
    """Call an internal service and translate its failures for API callers.

    By default every upstream failure becomes a sanitized 502/503/504.  Routes
    where the upstream 4xx is itself the meaningful caller outcome (restore
    challenge/confirmation refusals) opt into ``propagate_client_errors`` so the
    caller receives the rejection instead of a generic gateway error.
    """
    base = os.getenv(base_environment, "").rstrip("/")
    if not base:
        raise HTTPException(503, f"Сервіс не налаштований: {base_environment} не встановлено")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.request(
                method, f"{base}{path}", json=payload, params=params
            )
            response.raise_for_status()
            return response.json()
    except httpx.ConnectError:
        # Sanitized diagnostics: never expose the resolved internal URL.
        raise HTTPException(
            502, f"Сервіс {base_environment} недоступний. "
            "Перевірте, чи запущений відповідний сервіс."
        )
    except httpx.TimeoutException:
        raise HTTPException(
            504, f"Таймаут з'єднання з {base_environment}. Сервіс не відповідає."
        )
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        if propagate_client_errors and status in _CLIENT_ERROR_STATUSES:
            # Sanitized: no raw upstream response body or URL. The upstream
            # refusal status is the caller's own outcome, so it is preserved.
            raise HTTPException(
                status, f"Сервіс {base_environment} відхилив запит (HTTP {status})"
            )
        if propagate_client_errors and status in (401, 403):
            # An upstream credential refusal is not the caller losing a session,
            # so it surfaces as a conflict rather than an endpoint 401 logout.
            raise HTTPException(
                409,
                f"Сервіс {base_environment} відхилив внутрішній запит",
            )
        # Sanitized: no raw upstream response body or URL.
        raise HTTPException(
            502, f"Сервіс {base_environment} повернув помилку (HTTP {status})"
        )
    except (httpx.HTTPError, ValueError):
        # Sanitized: no raw transport error text (may embed the internal URL).
        raise HTTPException(
            503, f"Недоступний внутрішній сервіс: {base_environment}"
        )
