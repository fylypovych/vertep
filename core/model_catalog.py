"""Curated catalog of Ollama models available for download (#454).

The catalog ships with the release in ``core/ollama_catalog.json``. It lists library
tags confirmed against the Ollama registry, separate from the models installed on a
node. An unreadable or invalid catalog is reported, never replaced by guessed names.
"""
import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("ollama_catalog.json")
# Same rule as the pull API: the name is passed to Ollama unchanged.
MODEL_NAME = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:/+-]{0,127}")


def load_catalog(path: Path | None = None) -> dict:
    document = json.loads((path or CATALOG_PATH).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != 1 or not isinstance(document.get("models"), list):
        raise ValueError("invalid catalog document")
    models, seen = [], set()
    for item in document["models"]:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not MODEL_NAME.fullmatch(name) or ":" not in name or name in seen:
            raise ValueError(f"invalid catalog entry: {name!r}")
        if not isinstance(item.get("download_bytes"), int) or item["download_bytes"] <= 0:
            raise ValueError(f"invalid download size: {name}")
        seen.add(name)
        models.append({"name": name, "family": str(item.get("family") or ""),
                       "parameters": str(item.get("parameters") or ""),
                       "download_bytes": item["download_bytes"]})
    return {"source": str(document.get("source") or ""), "verified_at": str(document.get("verified_at") or ""),
            "models": models}
