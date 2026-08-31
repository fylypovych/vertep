#!/usr/bin/env python3
"""Build the signed update descriptor published beside a runtime bundle."""

import argparse
import hashlib
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.release_contract import sign_release_contract


def runtime_contract(package: Path) -> dict:
    with tarfile.open(package, "r:gz") as archive:
        member = next((item for item in archive.getmembers()
                       if item.name.lstrip("./") == "manifest.json"), None)
        if member is None or not member.isfile() or member.size > 1024 * 1024:
            raise ValueError("runtime package has no valid manifest.json")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError("runtime manifest cannot be read")
        value = json.loads(source.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runtime manifest must be an object")
    return value


def package_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def descriptor(package: Path, version: str, repository: str) -> dict:
    contract = runtime_contract(package)
    if contract.get("version") != version:
        raise ValueError("runtime package version does not match the release version")
    compatibility = contract.get("compatibility", {})
    return {
        "version": version,
        "channel": contract.get("channel", "stable"),
        "release_sequence": contract["release_sequence"],
        "issued_at": contract["issued_at"],
        "expires_at": contract["expires_at"],
        "min_version": compatibility.get("minimum_version", "0.0.0.1"),
        "required": False,
        "database": {
            "schema": compatibility["database_schema"],
            "strategy": compatibility["database_strategy"],
            "rollback_safe": compatibility["rollback_safe"],
        },
        "package": (
            f"https://github.com/{repository}/releases/download/{version}/"
            f"vertep-runtime-{version}.tar.gz"
        ),
        "sha256": package_sha256(package),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    signed = sign_release_contract(
        descriptor(args.package, args.version, args.repository), args.private_key)
    args.output.write_text(json.dumps(signed, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


if __name__ == "__main__":
    main()
