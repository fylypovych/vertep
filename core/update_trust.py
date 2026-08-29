"""Offline-root-signed release-key metadata for the Vertep update client."""

import base64
import hashlib
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


KEY_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def canonical_metadata(metadata: dict) -> bytes:
    unsigned = {key: value for key, value in metadata.items() if key != "signatures"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Root metadata {field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Root metadata {field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"Root metadata {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _verify_signature(message: bytes, encoded: str, public_key: Path) -> bool:
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError):
        return False
    with tempfile.TemporaryDirectory() as temporary:
        message_path = Path(temporary) / "metadata.json"
        signature_path = Path(temporary) / "signature.bin"
        message_path.write_bytes(message)
        signature_path.write_bytes(signature)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(public_key),
             "-signature", str(signature_path), str(message_path)],
            capture_output=True, check=False,
        )
    return result.returncode == 0


def validate_root_metadata(metadata: dict, root_keys: Path, trusted_version: int = 0,
                           trusted_sha256: str | None = None,
                           now: datetime | None = None) -> dict:
    """Validate threshold-signed key metadata and prevent metadata rollback."""
    if not isinstance(metadata, dict):
        raise ValueError("Root metadata must be an object")
    version = metadata.get("version")
    threshold = metadata.get("threshold")
    keys = metadata.get("release_keys")
    signatures = metadata.get("signatures")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Root metadata version must be a positive integer")
    if version < trusted_version:
        raise RuntimeError("Root metadata rollback was rejected")
    metadata_sha256 = hashlib.sha256(canonical_metadata(metadata)).hexdigest()
    if version == trusted_version and trusted_sha256 and metadata_sha256 != trusted_sha256:
        raise RuntimeError("Root metadata equivocates at a trusted version")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
        raise ValueError("Root metadata threshold must be a positive integer")
    if not isinstance(keys, dict) or not keys:
        raise ValueError("Root metadata has no release keys")
    if not isinstance(signatures, list):
        raise ValueError("Root metadata signatures must be a list")
    expires_at = _timestamp(metadata.get("expires_at"), "expires_at")
    if expires_at <= (now or datetime.now(timezone.utc)).astimezone(timezone.utc):
        raise RuntimeError("Root metadata has expired")

    message = canonical_metadata(metadata)
    verified: set[str] = set()
    for signature in signatures:
        if not isinstance(signature, dict):
            continue
        key_id = signature.get("key_id")
        if not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id) or key_id in verified:
            continue
        public_key = root_keys / f"{key_id}.pem"
        if public_key.is_file() and _verify_signature(message, signature.get("signature", ""), public_key):
            verified.add(key_id)
    if len(verified) < threshold:
        raise RuntimeError("Root metadata signature threshold was not met")

    normalized: dict[str, dict] = {}
    for key_id, value in keys.items():
        if not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id) or not isinstance(value, dict):
            raise ValueError("Root metadata contains an invalid release key")
        digest = value.get("sha256")
        channels = value.get("channels")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Release key digest is invalid")
        if not isinstance(channels, list) or not channels or any(
                not isinstance(channel, str) or not re.fullmatch(r"[a-z0-9-]{1,32}", channel)
                for channel in channels):
            raise ValueError("Release key channels are invalid")
        normalized[key_id] = {"sha256": digest, "channels": channels,
                              "revoked": value.get("revoked") is True}
    return {"version": version, "expires_at": expires_at.isoformat(),
            "metadata_sha256": metadata_sha256,
            "release_keys": normalized, "verified_root_keys": sorted(verified)}


def authorize_release_key(manifest: dict, metadata: dict, keyring: Path,
                          channel: str) -> Path:
    """Authorize a release key by id, channel, revocation state and pinned digest."""
    key_id = manifest.get("key_id")
    if not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id):
        raise ValueError("Manifest key_id is invalid")
    entry = metadata.get("release_keys", {}).get(key_id)
    if not isinstance(entry, dict):
        raise RuntimeError("Manifest release key is not trusted by root metadata")
    if entry.get("revoked") is True:
        raise RuntimeError("Manifest release key has been revoked")
    if channel not in entry.get("channels", []):
        raise RuntimeError("Manifest release key is not authorized for this channel")
    key_path = keyring / f"{key_id}.pem"
    if not key_path.is_file():
        raise RuntimeError("Authorized release key is unavailable")
    if hashlib.sha256(key_path.read_bytes()).hexdigest() != entry.get("sha256"):
        raise RuntimeError("Authorized release key digest does not match root metadata")
    return key_path
