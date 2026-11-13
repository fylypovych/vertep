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
    # Issue #122 P5: a GPU node also finishes the assembly of an external engine,
    # so it advertises video generation and video assembly.
    defaults = {"gpu": "image_generation,image_upscale,controlnet,inpainting,video_generation,video_assembly",
                "text": "text_generation", "voice": "speech_synthesis",
                "publisher": "publishing", "backup": "backup,snapshot,archive",
                "monitoring": "metrics,logs,alerting"}
    role = configured_role()
    raw = os.getenv("NODE_CAPABILITIES")
    if not raw:
        raw = defaults.get(role, "")
    return sorted({item.strip() for item in raw.split(",") if item.strip()})
