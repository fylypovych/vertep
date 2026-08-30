#!/usr/bin/env python3
"""Generate threshold-signed root metadata for Vertep update trust."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
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
                   expiry_days: int, channels: list[str]) -> dict:
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=expiry_days)).isoformat().replace("+00:00", "Z")
    release_keys = {}
    for key_id in key_ids:
        key_path = keys_dir / f"{key_id}.pem"
        digest = hashlib.sha256(key_path.read_bytes()).hexdigest()
        release_keys[key_id] = {"sha256": digest, "channels": channels}
    metadata = {
        "version": 1,
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
    parser = argparse.ArgumentParser(description="Generate root metadata")
    parser.add_argument("--keys-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--key-ids", required=True, nargs="+")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--expiry-days", type=int, default=365)
    parser.add_argument("--channels", nargs="+", default=["stable", "beta"])
    args = parser.parse_args()

    args.keys_dir.mkdir(parents=True, exist_ok=True)
    for key_id in args.key_ids:
        generate_key(key_id, args.keys_dir)

    metadata = build_metadata(
        args.key_ids, args.keys_dir, args.threshold,
        args.expiry_days, args.channels,
    )
    args.output.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
