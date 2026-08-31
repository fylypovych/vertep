"""Unprivileged status endpoint for the host-executed Update Agent."""

import json
import os
from pathlib import Path

from fastapi import FastAPI


app = FastAPI(title="Vertep Update Agent Status", version="1")


def _state() -> dict:
    path = Path(os.getenv("UPDATE_STATE_DIR", "/data/config/update")) / "status.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@app.get("/health")
def health() -> dict:
    state = _state()
    return {"status": "HEALTHY", "executor": "host-systemd",
            "update_state": state.get("state", "IDLE"),
            "phase": state.get("phase", "NORMAL")}
