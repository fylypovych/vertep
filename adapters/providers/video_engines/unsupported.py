"""Placeholder for a persisted engine Vertep no longer ships (#456)."""

from __future__ import annotations

from ..base import VideoEngine


class UnsupportedVideoEngine(VideoEngine):
    """A persisted selection of an engine Vertep no longer ships (#456).

    It is reported as selected and not configured, and every render is refused:
    it is never quietly replaced by Native (Issue #122 §9.12).
    """

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def provider(self) -> str:
        return self._name

    def configured(self) -> bool:
        return False

    def render(self, output, **kwargs):
        raise RuntimeError(f"Video engine {self._name!r} is not supported; select native or money-printer")
