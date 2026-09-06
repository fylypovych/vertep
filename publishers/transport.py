"""Testable HTTP transport for live Publisher adapters.

Publishers never talk to ``httpx`` directly — they go through ``HttpTransport``
so tests can inject ``FakeTransport`` and assert the exact request flow without
any network activity.
"""

from __future__ import annotations

import httpx


class HttpTransport:
    """Thin wrapper around httpx providing the verbs publishers need."""

    def post(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        content: bytes | None = None,
        timeout: float = 120,
    ) -> httpx.Response:
        return httpx.post(
            url, params=params, headers=headers, json=json,
            data=data, files=files, content=content, timeout=timeout,
        )

    def put(
        self,
        url: str,
        *,
        headers: dict | None = None,
        content: bytes | None = None,
        timeout: float = 120,
    ) -> httpx.Response:
        return httpx.put(url, headers=headers, content=content, timeout=timeout)

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float = 120,
    ) -> httpx.Response:
        return httpx.get(url, params=params, headers=headers, timeout=timeout)


class FakeTransport:
    """Records requests and returns pre-queued responses (for tests).

    Responses are consumed in order. Each is either an ``httpx.Response`` or a
    zero-arg callable that returns one, so tests can craft streaming/headers.
    """

    def __init__(self, responses: list | None = None) -> None:
        self.responses: list = list(responses or [])
        self.requests: list[tuple[str, str, dict]] = []

    def _next(self, method: str, url: str, kwargs: dict) -> httpx.Response:
        self.requests.append((method, url, dict(kwargs)))
        if not self.responses:
            raise AssertionError("FakeTransport: no more responses queued")
        response = self.responses.pop(0)
        if callable(response):
            return response()
        return response

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self._next("POST", url, kwargs)

    def put(self, url: str, **kwargs) -> httpx.Response:
        return self._next("PUT", url, kwargs)

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self._next("GET", url, kwargs)
