#!/usr/bin/env python3
"""Створення та перевірка підписаного контракту runtime-релізу."""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.release_contract import sign_release_contract, validate_release_contract


def _metadata(path: Path) -> dict:
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}


def build_contract(artifact_root: Path, version: str, sequence: int, channel: str,
                   role_catalog: str, image_lock: Path, sbom: str,
                   issued_at: datetime, validity_days: int,
                   compatibility: dict) -> dict:
    catalog_path = artifact_root / role_catalog
    sbom_path = artifact_root / sbom
    roles = json.loads(catalog_path.read_text(encoding="utf-8"))
    images = json.loads(image_lock.read_text(encoding="utf-8"))
    if not isinstance(roles, dict) or not isinstance(images, dict):
        raise ValueError("Role catalog and image lock must be JSON objects")

    required_services = sorted({service for role in roles.values()
                                for service in role.get("services", [])})
    missing_images = sorted(set(required_services) - set(images))
    if missing_images:
        raise ValueError("Image lock is missing role services: " + ", ".join(missing_images))

    files = {}
    for path in sorted(artifact_root.rglob("*")):
        if path.is_file():
            files[path.relative_to(artifact_root).as_posix()] = _metadata(path)
    contract = {
        "schema": 2,
        "product": "vertep",
        "version": version,
        "release_sequence": sequence,
        "channel": channel,
        "issued_at": issued_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at": (issued_at + timedelta(days=validity_days)).astimezone(
            timezone.utc).isoformat().replace("+00:00", "Z"),
        "compatibility": compatibility,
        "files": files,
        "images": images,
        "roles": {
            "catalog_file": role_catalog,
            "catalog_sha256": files[role_catalog]["sha256"],
            "profiles": {name: {
                "services": value.get("services", []),
                "capabilities": value.get("capabilities", []),
                "modules": value.get("modules", []),
            } for name, value in roles.items()},
        },
        "sbom": {"format": "CycloneDX-JSON", "file": sbom,
                 "sha256": files[sbom]["sha256"]},
    }
    validate_release_contract(contract, artifact_root=artifact_root,
                              now=issued_at + timedelta(seconds=1))
    return contract


def main() -> None:
    parser = argparse.ArgumentParser(description="Підписаний контракт runtime-релізу Vertep")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="створити й підписати контракт")
    build.add_argument("--artifact-root", type=Path, required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--sequence", type=int, required=True)
    build.add_argument("--channel", default="stable")
    build.add_argument("--roles", default="node_roles.json")
    build.add_argument("--image-lock", type=Path, required=True)
    build.add_argument("--sbom", default="sbom.cdx.json")
    build.add_argument("--private-key", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--validity-days", type=int, default=30)
    build.add_argument("--core-api", type=int, default=1)
    build.add_argument("--worker-api", type=int, default=1)
    build.add_argument("--database-schema", type=int, default=1)
    build.add_argument("--database-strategy", choices=("none", "expand", "contract"), default="none")
    build.add_argument("--rollback-safe", action="store_true")
    build.add_argument("--minimum-version", default="0.0.0.1")

    validate = subparsers.add_parser("validate", help="перевірити контракт і bundle")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--artifact-root", type=Path, required=True)
    validate.add_argument("--public-key", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "build":
        root = args.artifact_root.resolve()
        compatibility = {
            "core_api": args.core_api,
            "worker_api": args.worker_api,
            "database_schema": args.database_schema,
            "database_strategy": args.database_strategy,
            "rollback_safe": args.rollback_safe,
            "minimum_version": args.minimum_version,
        }
        contract = build_contract(root, args.version, args.sequence, args.channel,
                                  args.roles, args.image_lock, args.sbom,
                                  datetime.now(timezone.utc), args.validity_days,
                                  compatibility)
        signed = sign_release_contract(contract, args.private_key)
        args.output.write_text(json.dumps(signed, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        print(args.output)
    else:
        contract = json.loads(args.manifest.read_text(encoding="utf-8"))
        validate_release_contract(contract, args.artifact_root.resolve(), args.public_key)
        print(json.dumps({"valid": True, "version": contract["version"],
                          "release_sequence": contract["release_sequence"]}))


if __name__ == "__main__":
    main()
