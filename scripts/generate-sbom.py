#!/usr/bin/env python3
"""Створення мінімального CycloneDX SBOM для appliance-релізу Vertep."""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*(?:==|>=|<=|~=|>|<)?\s*([^,;\s]+)?")


def generate(root: Path, image_lock: Path, version: str) -> dict:
    components = []
    for raw in (root / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = REQUIREMENT_RE.match(line)
        if not match:
            raise ValueError(f"Unsupported Python requirement: {line}")
        name, dependency_version = match.groups()
        component = {"type": "library", "name": name, "scope": "required",
                     "properties": [{"name": "vertep:requirement", "value": line}]}
        if dependency_version:
            component["version"] = dependency_version
            component["purl"] = f"pkg:pypi/{name.lower()}@{dependency_version}"
        components.append(component)

    images = json.loads(image_lock.read_text(encoding="utf-8"))
    for service, image in sorted(images.items()):
        digest = image["digest"]
        components.append({
            "type": "container",
            "name": image["reference"],
            "version": digest,
            "bom-ref": f"{image['reference']}@{digest}",
            "hashes": [{"alg": "SHA-256", "content": digest.removeprefix("sha256:")}],
            "properties": [
                {"name": "vertep:service", "value": service},
                {"name": "vertep:platforms", "value": ",".join(image.get("platforms", []))},
            ],
        })
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "component": {"type": "application", "name": "vertep", "version": version},
        },
        "components": components,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Створити CycloneDX SBOM релізу Vertep")
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--image-lock", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = generate(args.root.resolve(), args.image_lock, args.version)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
