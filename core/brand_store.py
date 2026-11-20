"""Brands and channels for CORE (owner decision 19.11.2026, #500 AC1/AC2, #502).

The Brands Application Module is the single owner of brands, channels and
publication policies. While it runs (READY), every CORE consumer reads and
changes them through the module API. An installation that has not installed
the module yet keeps working from its legacy store (``BRANDS_ROOT`` files and
the CORE channel repository) until the administrator migrates into the module.
A READY module that does not answer fails closed instead of silently falling
back to stale legacy data.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import quote

import httpx

from .configuration import SAFE_ID, BrandConfig, read_json
from .models import Channel


class BrandStoreError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _root() -> Path:
    return Path(os.getenv("BRANDS_ROOT", "brands"))


def _module(method: str, route: str, *, params: dict | None = None, body: object | None = None):
    """``None`` when the Brands module is not in use; otherwise its JSON answer."""
    from .module.gateway import call_module
    try:
        answer = call_module("brands", method, route, params, body)
    except (httpx.HTTPError, ValueError) as error:
        raise BrandStoreError(503, "Brands module is unavailable") from error
    if answer is None:
        return None
    status, data = answer
    if status >= 400:
        detail = data.get("error") if isinstance(data, dict) else None
        raise BrandStoreError(status, str(detail or f"Brands module answered {status}"))
    return data


def module_active() -> bool:
    from .module.gateway import _ready_instance
    from .workflow.runtime import module_runtime
    runtime = module_runtime()
    return bool(runtime and _ready_instance(runtime.modules, "brands"))


# -- brands ----------------------------------------------------------------------------
def list_brands() -> list[dict]:
    data = _module("GET", "/brands")
    if data is not None:
        return list(data)
    result = []
    for path in sorted(_root().glob("*/brand.json")):
        try:
            result.append(BrandConfig.model_validate(read_json(path)).model_dump())
        except ValueError:
            continue
    return result


def get_brand(brand_id: str) -> dict | None:
    if not SAFE_ID.fullmatch(str(brand_id)):
        return None
    try:
        data = _module("GET", f"/brands/{quote(brand_id, safe='')}")
    except BrandStoreError as error:
        if error.status == 404:
            return None
        raise
    if data is not None:
        return dict(data)
    path = _root() / brand_id / "brand.json"
    return read_json(path) if path.is_file() else None


def brand_exists(brand_id: str) -> bool:
    return get_brand(brand_id) is not None


def create_brand(config: dict) -> dict:
    data = _module("POST", "/brands", body=config)
    if data is not None:
        return dict(data)
    directory = _root() / config["id"]
    if directory.exists():
        raise BrandStoreError(409, "Brand already exists")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brand.json").write_text(BrandConfig.model_validate(config).model_dump_json(indent=2), encoding="utf-8")
    return config


def update_brand(brand_id: str, config: dict) -> dict:
    data = _module("PUT", f"/brands/{quote(brand_id, safe='')}", body=config)
    if data is not None:
        return dict(data)
    directory = _root() / brand_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brand.json").write_text(BrandConfig.model_validate(config).model_dump_json(indent=2), encoding="utf-8")
    return config


def delete_brand(brand_id: str) -> None:
    if _module("DELETE", f"/brands/{quote(brand_id, safe='')}") is not None:
        return
    directory = _root() / brand_id
    if not directory.is_dir():
        raise BrandStoreError(404, "Brand not found")
    shutil.rmtree(directory)


# -- channels --------------------------------------------------------------------------
def _channel(item: dict) -> Channel:
    return Channel(channel_id=item["channel_id"], brand_id=item["brand_id"], channel_type=item["channel_type"],
                   target=item["target"], enabled=bool(item.get("enabled", True)),
                   metadata=item.get("metadata") or {}, created_at=item.get("created_at") or Channel.model_fields["created_at"].default_factory())


def list_channels(brand_id: str | None = None) -> list[Channel]:
    data = _module("GET", "/channels", params={"brand_id": brand_id} if brand_id else None)
    if data is not None:
        return [_channel(item) for item in data]
    from .state import store
    return store.repository.list_channels(brand_id) if brand_id else store.repository.list_channels()


def get_channel(channel_id: str) -> Channel | None:
    try:
        data = _module("GET", f"/channels/{quote(channel_id, safe='')}")
    except BrandStoreError as error:
        if error.status == 404:
            return None
        raise
    if data is not None:
        return _channel(data)
    from .state import store
    return store.repository.get_channel(channel_id)


def create_channel(channel: Channel) -> Channel:
    body = {"channel_type": channel.channel_type, "target": channel.target, "enabled": channel.enabled,
            "metadata": channel.metadata}
    data = _module("POST", f"/brands/{quote(channel.brand_id, safe='')}/channels", body=body)
    if data is not None:
        return _channel(data)
    from .state import store
    store.repository.save_channel(channel)
    return channel


def update_channel(channel: Channel) -> Channel:
    body = {"target": channel.target, "enabled": channel.enabled, "metadata": channel.metadata}
    data = _module("PUT", f"/channels/{quote(channel.channel_id, safe='')}", body=body)
    if data is not None:
        return _channel(data)
    from .state import store
    store.repository.save_channel(channel)
    return channel


def delete_channel(channel_id: str) -> None:
    if _module("DELETE", f"/channels/{quote(channel_id, safe='')}") is not None:
        return
    from .state import store
    store.repository.delete_channel(channel_id)


# -- migration into the module -----------------------------------------------------------
def migrate_to_module() -> dict:
    """Copy the legacy brands/channels into the running Brands module (idempotent,
    IDs kept). The legacy files stay untouched; the module is authoritative from now."""
    if not module_active():
        raise BrandStoreError(409, "Brands module is not active")
    from .state import store
    brands = []
    for path in sorted(_root().glob("*/brand.json")):
        try:
            brands.append(BrandConfig.model_validate(read_json(path)).model_dump())
        except ValueError:
            continue
    channels = [channel.model_dump() for channel in store.repository.list_channels()]
    return _module("POST", "/migrate", body={"brands": brands, "channels": channels})
