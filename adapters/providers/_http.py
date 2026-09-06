"""Shared HTTP helpers for provider backends that talk to remote services.

Providers never talk to ``httpx`` for correctness checks themselves — they go
through ``check_response`` so the exact error-signalling contract (``HTTP <code>``)
is identical across compute / video-engine backends and is easy to test.
"""

from __future__ import annotations


def check_response(response) -> None:
    """Raise a ``RuntimeError`` describing the HTTP failure, if any."""
    if response.status_code >= 400:
        detail = ""
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else None
            detail = (
                str(error.get("message", error))
                if isinstance(error, dict)
                else str(error or data)
            )
        except Exception:  # noqa: BLE001
            detail = response.text[:200]
        raise RuntimeError(
            f"HTTP {response.status_code} ({response.reason_phrase}): {detail}"
        )