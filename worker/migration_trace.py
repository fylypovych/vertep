"""Replay external provider responses while executing real shadow business logic."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import httpx

_trace = ContextVar("migration_trace", default=None)


def replaying():
    value = _trace.get()
    return bool(value and value["replay"])


@contextmanager
def recording(entries, replay=False):
    context = {"entries": entries, "replay": replay, "position": 0}
    token = _trace.set(context)
    try:
        yield
        if replay and context["position"] != len(entries):
            raise RuntimeError("Shadow did not consume the complete provider trace")
    finally:
        _trace.reset(token)


def external(key, call):
    context = _trace.get()
    if context is None:
        return call()
    if context["replay"]:
        index = context["position"]
        if index >= len(context["entries"]) or context["entries"][index][0] != key:
            raise RuntimeError("Shadow attempted an unrecorded provider side effect")
        context["position"] += 1
        return deepcopy(context["entries"][index][1])
    result = call()
    context["entries"].append((deepcopy(key), deepcopy(result)))
    return result


def post(client, endpoint, **kwargs):
    if _trace.get() is None:
        return client.post(endpoint, **kwargs)
    def invoke():
        response = client.post(endpoint, **kwargs)
        response.raise_for_status()
        return response.status_code, dict(response.headers), response.content
    status, headers, content = external(("POST", endpoint, kwargs.get("json")), invoke)
    return httpx.Response(status, headers=headers, content=content, request=httpx.Request("POST", endpoint))


@contextmanager
def stream(client, method, endpoint, **kwargs):
    if _trace.get() is None:
        with client.stream(method, endpoint, **kwargs) as response:
            yield response
        return
    def invoke():
        with client.stream(method, endpoint, **kwargs) as response:
            response.raise_for_status()
            return response.status_code, dict(response.headers), response.read()
    status, headers, content = external((method, endpoint, kwargs.get("json")), invoke)
    yield httpx.Response(status, headers=headers, content=content, request=httpx.Request(method, endpoint))
