#!/usr/bin/env python3
"""Assemble the immutable service image lock from build metadata artifacts."""

import argparse
import json
from pathlib import Path


ALIASES = {
    "core": ("core", "worker", "license-manager", "dispatcher", "scheduler",
             "certificate-manager", "migrate", "update-agent"),
    "publisher-worker": ("publisher-worker",),
    "backup-service": ("backup-service",),
}


def assemble(source: Path) -> dict:
    built = {}
    for path in sorted(source.glob("**/*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"Invalid image metadata: {path}")
        if name in built:
            raise ValueError(f"Duplicate image metadata: {name}")
        built[name] = {key: item[key] for key in ("reference", "digest", "platforms")}
    if not built:
        raise ValueError("No image metadata artifacts found")
    result = dict(built)
    for source_name, aliases in ALIASES.items():
        if source_name not in built:
            raise ValueError(f"Missing built image: {source_name}")
        for alias in aliases:
            result[alias] = dict(built[source_name])
    return dict(sorted(result.items()))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(assemble(args.source), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
