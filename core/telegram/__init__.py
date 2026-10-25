"""Telegram module for CORE composition root (Issue #110 Phase D).

Telegram business logic (callback handlers, message handlers, polling) lives
here, extracted from ``core/app.py``.  Modules in this package must NOT import
``core.app`` (Acceptance 4) — they import directly from ``core.state``,
``core.operations``, ``core.pipeline`` etc.
"""

from __future__ import annotations

from ..telegram_callbacks import DurableCallbackSet

# Module-level globals (migrated from core/app.py).
telegram_polling_service = None
_telegram_system_callbacks = DurableCallbackSet()
_telegram_system_restore: dict[str, dict] = {}

__all__ = [
    "telegram_polling_service",
    "_telegram_system_callbacks",
    "_telegram_system_restore",
]
