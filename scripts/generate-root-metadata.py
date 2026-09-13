#!/usr/bin/env python3
"""Generate threshold-signed root metadata for Vertep update trust."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path


KEY_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
DEFAULT_THRESHOLD = 2


def canonical_metadata(metadata: dict) -> bytes:
    unsigned = {key: value for key, value in metadata.items() if key != "signatures"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def generate_key(key_id: str, directory: Path) -> Path:
    key_path = directory / f"{key_id}.pem"
    if key_path.exists():
        return key_path
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:4096",
         "-out", str(key_path)],
        check=True, capture_output=True,
    )
    return key_path


def sign_metadata(message: bytes, key_path: Path) -> str:
    with subprocess.Popen(
        ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, check=True,
    ) as process:
        stdout, _ = process.communicate(message)
    return __import__("base64").b64encode(stdout).decode("ascii")


def build_metadata(key_ids: list[str], keys_dir: Path, threshold: int,
                   expiry_days: int, channels: list[str], version: int = 1,
                   revoked: set[str] | None = None,
                   existing_metadata: dict | None = None) -> dict:
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Root metadata version must be a positive integer")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
        raise ValueError("Root metadata threshold must be a positive integer")
    if expiry_days < 1:
        raise ValueError("Root metadata expiry must be positive")
    if not key_ids:
        raise ValueError("At least one root key is required")
    if threshold > len(key_ids):
        raise ValueError("Root metadata threshold exceeds the signing key count")

    existing_keys = {}
    if existing_metadata is not None:
        if not isinstance(existing_metadata, dict):
            raise ValueError("Existing root metadata must be an object")
        existing_version = existing_metadata.get("version")
        if not isinstance(existing_version, int) or isinstance(existing_version, bool) or existing_version < 1:
            raise ValueError("Existing root metadata version is invalid")
        if version <= existing_version:
            raise ValueError("Root metadata version must increase during rotation")
        existing_keys = existing_metadata.get("release_keys")
        if not isinstance(existing_keys, dict):
            raise ValueError("Existing root metadata has no release keys")

    revoked_ids = set(revoked or ())
    release_keys = {}
    for key_id in key_ids:
        if not KEY_ID_RE.fullmatch(key_id):
            raise ValueError(f"Invalid root key id: {key_id}")
        key_path = keys_dir / f"{key_id}.pem"
        if not key_path.is_file():
            raise FileNotFoundError(key_path)
        previous = existing_keys.get(key_id, {}) if isinstance(existing_keys.get(key_id), dict) else {}
        entry_channels = previous.get("channels", channels)
        if not isinstance(entry_channels, list) or not entry_channels:
            raise ValueError(f"Root key {key_id} has invalid channels")
        release_keys[key_id] = {
            "sha256": hashlib.sha256(key_path.read_bytes()).hexdigest(),
            "channels": entry_channels,
            "revoked": key_id in revoked_ids or previous.get("revoked") is True,
        }

    for key_id, value in existing_keys.items():
        if key_id in release_keys or not isinstance(value, dict):
            continue
        release_keys[key_id] = {
            "sha256": value.get("sha256"),
            "channels": value.get("channels", []),
            "revoked": key_id in revoked_ids or value.get("revoked") is True,
        }
    if not any(not value["revoked"] for value in release_keys.values()):
        raise ValueError("Root metadata must retain at least one active release key")

    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=expiry_days)).isoformat().replace("+00:00", "Z")
    metadata = {
        "version": version,
        "threshold": threshold,
        "expires_at": expires_at,
        "release_keys": release_keys,
    }
    message = canonical_metadata(metadata)
    signatures = []
    for key_id in key_ids:
        key_path = keys_dir / f"{key_id}.pem"
        signatures.append({
            "key_id": key_id,
            "signature": sign_metadata(message, key_path),
        })
    metadata["signatures"] = signatures
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or rotate root metadata")
    parser.add_argument("--keys-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--key-ids", required=True, nargs="+")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--expiry-days", type=int, default=365)
    parser.add_argument("--channels", nargs="+", default=["stable", "beta"])
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--revoked", nargs="*", default=[])
    parser.add_argument("--existing-metadata", type=Path)
    args = parser.parse_args()

    args.keys_dir.mkdir(parents=True, exist_ok=True)
    for key_id in args.key_ids:
        generate_key(key_id, args.keys_dir)

    existing_metadata = None
    if args.existing_metadata:
        existing_metadata = json.loads(args.existing_metadata.read_text(encoding="utf-8"))
    metadata = build_metadata(
        args.key_ids, args.keys_dir, args.threshold, args.expiry_days,
        args.channels, version=args.version, revoked=set(args.revoked),
        existing_metadata=existing_metadata,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
