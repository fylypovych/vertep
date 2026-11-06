"""Platform credentials for the publish task running on this node.

A node has no database access: CORE owns the store and hands out only the secrets of
the channel of the task this node currently runs. Values stay in memory for the task.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import httpx

_current: ContextVar["TaskSecrets | None"] = ContextVar("task_secrets", default=None)


class TaskSecrets:
    def __init__(self, url: str, headers: dict) -> None:
        self._url, self._headers = url, headers
        self._loaded: dict | None = None

    def _load(self) -> dict:
        if self._loaded is None:
            response = httpx.get(self._url, headers=self._headers, timeout=30)
            response.raise_for_status()
            self._loaded = response.json()
        return self._loaded

    def get(self, name: str):
        return self._load().get("secrets", {}).get(name.lower())

    def oauth(self, credential_env: str) -> dict:
        return self._load().get("oauth", {}).get(credential_env, {})

    def save(self, secrets: dict, oauth: dict) -> None:
        response = httpx.put(self._url, headers=self._headers, json={"secrets": secrets, "oauth": oauth}, timeout=30)
        response.raise_for_status()
        current = self._load()
        current.setdefault("secrets", {}).update(secrets)
        current.setdefault("oauth", {}).update(oauth)


def current_task_secrets() -> TaskSecrets | None:
    return _current.get()


@contextmanager
def task_secrets(source: TaskSecrets | None):
    token = _current.set(source)
    try:
        yield
    finally:
        _current.reset(token)
