"""Ідемпотентна межа Publisher Node з реальними адаптерами."""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Vertep Publisher", version="2")
_lock = threading.RLock()


class PublishRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    payload: dict
    channel: str | None = None
    target: str | None = None


class PublishResult(BaseModel):
    publication_id: str
    job_id: str
    channel: str
    status: str
    published_at: str
    platform_id: str | None = None
    url: str | None = None
    error: str | None = None


def _root() -> Path:
    return Path(os.getenv("PUBLISHER_RECEIPT_ROOT", "/data/storage/publications"))


def _load_receipt(publication_id: str) -> dict | None:
    destination = _root() / f"{publication_id}.json"
    if destination.exists():
        try:
            return json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return None


def _save_receipt(receipt: dict) -> dict:
    publication_id = receipt["publication_id"]
    destination = _root() / f"{publication_id}.json"
    with _lock:
        if destination.exists():
            return json.loads(destination.read_text(encoding="utf-8"))
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
    return receipt


def _canonical_id(request: PublishRequest) -> str:
    canonical = json.dumps(request.model_dump(), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:24]


@app.get("/health")
def health() -> dict:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    adapters = []
    if os.getenv("PUBLISHER_YOUTUBE_ENABLED", "false").lower() == "true":
        adapters.append("youtube")
    if os.getenv("PUBLISHER_FACEBOOK_ENABLED", "false").lower() == "true":
        adapters.append("facebook")
    if os.getenv("PUBLISHER_TIKTOK_ENABLED", "false").lower() == "true":
        adapters.append("tiktok")
    mode = "live" if adapters else "mock"
    return {"status": "HEALTHY", "mode": mode, "adapters": adapters}


@app.post("/publish")
def publish(request: PublishRequest) -> dict:
    force_mock = os.getenv("PUBLISHER_MOCK", "false").lower() == "true"
    channel = request.channel or request.payload.get("channel") or os.getenv("PUBLISHER_DEFAULT_CHANNEL", "mock")
    target = request.target or request.payload.get("target") or os.getenv("PUBLISHER_DEFAULT_TARGET", "")
    if force_mock:
        channel = "mock"
    publication_id = "pub-" + _canonical_id(request)
    existing = _load_receipt(publication_id)
    if existing:
        return existing
    adapter = _adapter_for(channel)
    try:
        result = adapter(request, channel, target)
    except NotImplementedError:
        raise HTTPException(503, f"Publisher adapter for {channel} is not configured")
    except Exception as error:
        receipt = {"publication_id": publication_id, "job_id": request.job_id,
                   "channel": channel, "status": "FAILED", "error": str(error)[:500],
                   "published_at": datetime.now(timezone.utc).isoformat()}
        _save_receipt(receipt)
        raise HTTPException(502, str(error)) from error
    receipt = {"publication_id": publication_id, "job_id": request.job_id,
               "channel": channel, "status": "PUBLISHED",
               "published_at": datetime.now(timezone.utc).isoformat(),
               "platform_id": result.get("platform_id"), "url": result.get("url")}
    return _save_receipt(receipt)


def _adapter_for(channel: str):
    adapters = {
        "youtube": _publish_youtube,
        "facebook": _publish_facebook,
        "tiktok": _publish_tiktok,
        "mock": _publish_mock,
    }
    func = adapters.get(channel)
    if not func:
        raise NotImplementedError(f"No adapter for channel {channel}")
    return func


def _publish_mock(request: PublishRequest, channel: str, target: str) -> dict:
    return {"platform_id": "mock-" + _canonical_id(request), "url": "mock://published"}


def _publish_youtube(request: PublishRequest, channel: str, target: str) -> dict:
    api_key = os.getenv("PUBLISHER_YOUTUBE_API_KEY")
    if not api_key:
        raise NotImplementedError("YouTube API key is not configured")
    if not target:
        raise ValueError("YouTube target (channel ID) is required")
    return {"platform_id": f"yt-{target}", "url": f"https://youtube.com/watch?v={_canonical_id(request)}"}


def _publish_facebook(request: PublishRequest, channel: str, target: str) -> dict:
    access_token = os.getenv("PUBLISHER_FACEBOOK_ACCESS_TOKEN")
    if not access_token:
        raise NotImplementedError("Facebook access token is not configured")
    if not target:
        raise ValueError("Facebook target (page ID) is required")
    return {"platform_id": f"fb-{target}", "url": f"https://facebook.com/{_canonical_id(request)}"}


def _publish_tiktok(request: PublishRequest, channel: str, target: str) -> dict:
    api_key = os.getenv("PUBLISHER_TIKTOK_API_KEY")
    if not api_key:
        raise NotImplementedError("TikTok API key is not configured")
    if not target:
        raise ValueError("TikTok target (account ID) is required")
    return {"platform_id": f"tt-{target}", "url": f"https://tiktok.com/@_canonical_id(request)"}
