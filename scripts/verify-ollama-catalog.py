#!/usr/bin/env python3
"""Confirm every tag of core/ollama_catalog.json against the Ollama registry (#454).

Run before a release that changes or refreshes the catalog. A missing tag or a changed
download size fails, so the catalog never lists names that cannot be pulled.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.model_catalog import CATALOG_PATH, load_catalog  # noqa: E402

REGISTRY = "https://registry.ollama.ai/v2/library"
ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"


def manifest_size(name: str, timeout: float = 30) -> int | None:
    """Download size of a library tag, or None when the registry does not know it."""
    model, tag = name.split(":", 1)
    request = urllib.request.Request(f"{REGISTRY}/{model}/manifests/{tag}", headers={"Accept": ACCEPT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    return sum(layer.get("size", 0) for layer in document.get("layers", [])) + document.get("config", {}).get("size", 0)


def verify(catalog: dict, fetch=manifest_size) -> list[str]:
    problems = []
    for item in catalog["models"]:
        size = fetch(item["name"])
        if size is None:
            problems.append(f"{item['name']}: tag is not in the Ollama registry")
        elif size != item["download_bytes"]:
            problems.append(f"{item['name']}: download size changed {item['download_bytes']} -> {size}")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    args = parser.parse_args(argv)
    problems = verify(load_catalog(args.catalog))
    for problem in problems:
        print(problem, file=sys.stderr)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
