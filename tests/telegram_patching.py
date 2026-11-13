"""Replace a Telegram dependency in every ``core.telegram`` module that binds it.

``core.telegram.handlers`` routes callbacks and re-exports the handlers that live
in ``common``, ``status``, ``system_ops`` and ``job_callbacks``.  Each module
resolves its dependencies (``TelegramAdapter``, ``_call_core_api``, operation
helpers, ``store``) through its own globals, so a test that stubs one of them
patches it wherever it is bound.
"""

import core.telegram.common as common
import core.telegram.handlers as handlers
import core.telegram.job_callbacks as job_callbacks
import core.telegram.status as status
import core.telegram.system_ops as system_ops

TELEGRAM_MODULES = (handlers, common, status, system_ops, job_callbacks)


def patch_telegram(monkeypatch, name: str, value) -> None:
    """``monkeypatch.setattr`` ``name`` in each Telegram module that defines it."""
    owners = [module for module in TELEGRAM_MODULES if hasattr(module, name)]
    assert owners, f"no core.telegram module binds {name!r}"
    for module in owners:
        monkeypatch.setattr(module, name, value)
