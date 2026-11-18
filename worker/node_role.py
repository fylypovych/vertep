"""Role and capabilities this Worker node advertises."""

import json
import os
from pathlib import Path


def configured_role() -> str:
    role = os.getenv("NODE_ROLE", "gpu")
    if role != "unassigned":
        return role
    try:
        return str(json.loads((Path(os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json")).parent
                               / "installation.json").read_text())["node_role"])
    except (OSError, ValueError, KeyError):
        return "gpu"

def node_capabilities() -> list[str]:
    # Assembly is CPU work; both CPU Text and GPU Workers may execute it.
    defaults = {"gpu": "image_generation,image_upscale,controlnet,inpainting,video_generation,video_assembly",
                "text": "text_generation,video_assembly", "voice": "speech_synthesis",
                "publisher": "publishing", "backup": "backup,snapshot,archive",
                "monitoring": "metrics,logs,alerting"}
    role = configured_role()
    raw = os.getenv("NODE_CAPABILITIES")
    if not raw:
        raw = defaults.get(role, "")
    return sorted({item.strip() for item in raw.split(",") if item.strip()})


def supported_tasks() -> list[str]:
    defaults = {
        "gpu": "image,video,assembly",
        "text": "text,script,storyboard,assembly",
        "voice": "voice",
        "publisher": "publish",
        "backup": "backup",
    }
    raw = os.getenv("SUPPORTED_TASKS") or defaults.get(configured_role(), "")
    return list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def requires_gpu() -> bool:
    default = "true" if configured_role() == "gpu" else "false"
    return (os.getenv("WORKER_REQUIRE_GPU", default).lower() == "true"
            and os.getenv("DEMO_MODE", "false").lower() != "true")
