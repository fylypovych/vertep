"""Ідемпотентна межа Publisher Node без удаваних успішних публікацій."""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Vertep Publisher", version="1")
_lock = threading.RLock()


class PublishRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    payload: dict


def _root() -> Path:
    return Path(os.getenv("PUBLISHER_RECEIPT_ROOT", "/data/storage/publications"))


@app.get("/health")
def health() -> dict:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    return {"status": "HEALTHY", "mode": "mock" if os.getenv(
        "PUBLISHER_MOCK", "false").lower() == "true" else "live"}


@app.post("/publish")
def publish(request: PublishRequest) -> dict:
    if os.getenv("PUBLISHER_MOCK", "false").lower() != "true":
        raise HTTPException(503, "Жоден live publisher adapter не налаштовано")
    canonical = json.dumps(request.model_dump(), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    publication_id = "mock-" + hashlib.sha256(canonical).hexdigest()[:24]
    receipt = {"publication_id": publication_id, "job_id": request.job_id,
               "status": "PUBLISHED", "channel": "mock",
               "published_at": datetime.now(timezone.utc).isoformat()}
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{publication_id}.json"
    with _lock:
        if destination.exists():
            return json.loads(destination.read_text(encoding="utf-8"))
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
    return receipt
