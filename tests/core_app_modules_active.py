"""CORE for process tests that run without module containers: official modules count as READY.

Their pages and CORE API exist only with a READY module (core/module/navigation.py); the
in-process suite gets the same allowance from tests/conftest.py. Production has none.
"""
from core.module import navigation
from tests.module_menu import with_official_pages

_real = navigation.navigation
navigation.module_ready = lambda module_id: True
navigation.navigation = lambda: with_official_pages(_real())

from core.app import app  # noqa: E402  (after the allowance)

__all__ = ["app"]
