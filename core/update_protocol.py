"""Validation and download primitives for signed Vertep release packages."""

import base64
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .version import application_version


VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){2,3}$")


def version_tuple(value: str) -> tuple[int, ...]:
    if not VERSION_RE.fullmatch(value):
        raise ValueError(f"Invalid release version: {value}")
    return tuple(int(part) for part in value.split("."))


def canonical_manifest(manifest: dict) -> bytes:
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _manifest_key(public_key: Path, key_id: str | None) -> Path:
    if public_key.is_dir():
        if not key_id or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", key_id):
            raise ValueError("Manifest key_id is required for the release keyring")
        public_key = public_key / f"{key_id}.pem"
    return public_key


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Manifest {field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Manifest {field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"Manifest {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_manifest(manifest: dict, public_key: Path, current: str | None = None,
                      expected_channel: str | None = None, now: datetime | None = None) -> dict:
    required = {"version", "package", "sha256", "signature"}
    if not required.issubset(manifest):
        raise ValueError(f"Manifest is missing: {', '.join(sorted(required - manifest.keys()))}")
    current = current or application_version()
    version_tuple(manifest["version"])
    current_version = version_tuple(current)
    if manifest.get("min_version") and current_version < version_tuple(manifest["min_version"]):
        raise RuntimeError("Current version is older than the supported upgrade path")
    if manifest.get("max_version") and current_version > version_tuple(manifest["max_version"]):
        raise RuntimeError("Current version is newer than the supported upgrade path")
    if expected_channel is not None and manifest.get("channel") != expected_channel:
        raise RuntimeError("Update manifest channel does not match the requested channel")
    if manifest.get("issued_at") or manifest.get("expires_at"):
        issued_at = _parse_utc(manifest.get("issued_at"), "issued_at")
        expires_at = _parse_utc(manifest.get("expires_at"), "expires_at")
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if expires_at <= issued_at:
            raise ValueError("Manifest expires_at must be later than issued_at")
        if issued_at > current_time:
            raise RuntimeError("Update manifest is not valid yet")
        if expires_at <= current_time:
            raise RuntimeError("Update manifest has expired")
    elif os.getenv("REQUIRE_UPDATE_KEY_LIFECYCLE", "false").lower() == "true":
        raise ValueError("Manifest validity window is required")
    sequence = manifest.get("release_sequence")
    if sequence is not None and (not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1):
        raise ValueError("Manifest release_sequence must be a positive integer")
    if sequence is None and os.getenv("REQUIRE_UPDATE_KEY_LIFECYCLE", "false").lower() == "true":
        raise ValueError("Manifest release_sequence is required")
    database = manifest.get("database")
    if database is not None:
        if not isinstance(database, dict) or database.get("strategy") not in {"none", "expand", "contract"}:
            raise ValueError("Manifest database strategy is invalid")
        if not isinstance(database.get("schema"), int) or database["schema"] < 0:
            raise ValueError("Manifest database schema must be a non-negative integer")
        if not isinstance(database.get("rollback_safe"), bool):
            raise ValueError("Manifest database rollback_safe must be boolean")
        if (os.getenv("REQUIRE_ROLLING_COMPATIBILITY", "false").lower() == "true"
                and database["strategy"] == "contract"):
            raise RuntimeError("Contract migrations are forbidden during a rolling-compatible update")
    elif os.getenv("REQUIRE_ROLLING_COMPATIBILITY", "false").lower() == "true":
        raise ValueError("Manifest database compatibility metadata is required")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest["sha256"])):
        raise ValueError("Invalid package SHA-256")
    try:
        signature = base64.b64decode(manifest["signature"], validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid manifest signature encoding") from error
    public_key = _manifest_key(public_key, manifest.get("key_id"))
    if not public_key.is_file():
        raise RuntimeError("Update verification public key is unavailable")
    with tempfile.TemporaryDirectory() as temporary:
        message = Path(temporary) / "manifest.json"
        signature_file = Path(temporary) / "signature.bin"
        message.write_bytes(canonical_manifest(manifest))
        signature_file.write_bytes(signature)
        result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public_key),
                                 "-signature", str(signature_file), str(message)],
                                capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Update manifest signature verification failed")
    return manifest


def validate_replay_state(manifest: dict, trusted: dict | None) -> dict:
    """Reject rollback/equivocation while allowing repeated checks of the same release."""
    sequence = manifest.get("release_sequence")
    if sequence is None:
        return {}
    candidate = {"release_sequence": sequence, "version": manifest["version"],
                 "sha256": manifest["sha256"], "key_id": manifest.get("key_id")}
    if not trusted:
        return candidate
    previous = trusted.get("release_sequence")
    if not isinstance(previous, int):
        raise RuntimeError("Stored update replay state is invalid")
    if sequence < previous:
        raise RuntimeError("Update manifest release sequence was replayed")
    if sequence == previous and any(trusted.get(key) != candidate.get(key)
                                    for key in ("version", "sha256", "key_id")):
        raise RuntimeError("Update manifest equivocates at an accepted release sequence")
    return candidate if sequence > previous else trusted


def update_server_url() -> str:
    url = os.getenv("VERTEP_UPDATE_SERVER", "https://update.vertep.ai").rstrip("/") + "/"
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("VERTEP_UPDATE_SERVER must be an HTTPS origin without credentials")
    return url


def _read_small_response(request: urllib.request.Request, *, timeout: int,
                         maximum: int) -> bytes:
    attempts = max(1, int(os.getenv("UPDATE_HTTP_RETRIES", "3")))
    delay = max(0.0, float(os.getenv("UPDATE_HTTP_RETRY_DELAY_SECONDS", "2")))
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout, context=ssl.create_default_context()) as response:
                if response.status != 200:
                    raise RuntimeError(f"Update server returned HTTP {response.status}")
                return response.read(maximum + 1)
        except (TimeoutError, socket.timeout, urllib.error.URLError) as error:
            if attempt == attempts:
                raise RuntimeError(
                    f"Не вдалося прочитати відповідь сервера оновлень після {attempts} спроб: {error}"
                ) from error
            time.sleep(delay)
    raise AssertionError("unreachable")


def fetch_manifest(channel: str = "stable") -> dict:
    if not re.fullmatch(r"[a-z0-9-]{1,32}", channel):
        raise ValueError("Invalid update channel")
    base = update_server_url()
    parsed = urlsplit(base)
    github_repository = re.fullmatch(r"/repos/([^/]+)/([^/]+)/releases/", parsed.path)
    url = (urljoin(base, "latest") if parsed.hostname == "api.github.com" and github_repository
           else urljoin(base, f"v1/releases/{channel}/latest.json"))
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Vertep-Updater/1"})
    payload = _read_small_response(request, timeout=30, maximum=1024 * 1024)
    if len(payload) > 1024 * 1024:
        raise RuntimeError("Update manifest is too large")
    document = json.loads(payload)
    if parsed.hostname == "api.github.com" and github_repository:
        if not isinstance(document, dict):
            raise ValueError("GitHub release metadata must be an object")
        version = str(document.get("tag_name", "")).removeprefix("v")
        expected_name = f"update-manifest-{version}.json"
        asset = next((item for item in document.get("assets", [])
                      if isinstance(item, dict) and item.get("name") == expected_name), None)
        if asset is None or not isinstance(asset.get("url"), str):
            raise RuntimeError(f"GitHub release has no {expected_name}")
        asset_request = urllib.request.Request(
            asset["url"], headers={"Accept": "application/octet-stream",
                                    "User-Agent": "Vertep-Updater/1"})
        payload = _read_small_response(asset_request, timeout=30, maximum=1024 * 1024)
        if len(payload) > 1024 * 1024:
            raise RuntimeError("Update manifest is too large")
        manifest = json.loads(payload)
    else:
        manifest = document
    if not isinstance(manifest, dict):
        raise ValueError("Update manifest must be an object")
    return manifest


def download_package(manifest: dict, destination: Path) -> Path:
    base = update_server_url()
    url = urljoin(base, manifest["package"])
    parsed_base, parsed_url = urlsplit(base), urlsplit(url)
    github_repository = re.fullmatch(r"/repos/([^/]+)/([^/]+)/releases/", parsed_base.path)
    github_download = bool(
        parsed_base.hostname == "api.github.com" and github_repository
        and parsed_url.hostname == "github.com" and parsed_url.scheme == "https"
        and parsed_url.path.startswith(
            f"/{github_repository.group(1)}/{github_repository.group(2)}/releases/download/"))
    same_origin = parsed_url.hostname == parsed_base.hostname and parsed_url.scheme == "https"
    if parsed_url.username or parsed_url.password or not (same_origin or github_download):
        raise RuntimeError("Package URL is outside the configured update server")
    digest = hashlib.sha256()
    maximum = int(os.getenv("UPDATE_MAX_PACKAGE_BYTES", str(4 * 1024**3)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Vertep-Updater/1"})
    total = 0
    with urllib.request.urlopen(request, timeout=60, context=ssl.create_default_context()) as response, \
            destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > maximum:
                raise RuntimeError("Update package exceeds configured size limit")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != manifest["sha256"]:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Update package checksum mismatch")
    return destination
