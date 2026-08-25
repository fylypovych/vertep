"""Validation and download primitives for signed Vertep release packages."""

import base64
import hashlib
import json
import os
import re
import ssl
import subprocess
import tempfile
import urllib.request
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


def validate_manifest(manifest: dict, public_key: Path, current: str | None = None) -> dict:
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
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest["sha256"])):
        raise ValueError("Invalid package SHA-256")
    try:
        signature = base64.b64decode(manifest["signature"], validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid manifest signature encoding") from error
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


def update_server_url() -> str:
    url = os.getenv("VERTEP_UPDATE_SERVER", "https://update.vertep.ai").rstrip("/") + "/"
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("VERTEP_UPDATE_SERVER must be an HTTPS origin without credentials")
    return url


def fetch_manifest(channel: str = "stable") -> dict:
    if not re.fullmatch(r"[a-z0-9-]{1,32}", channel):
        raise ValueError("Invalid update channel")
    request = urllib.request.Request(urljoin(update_server_url(), f"v1/releases/{channel}/latest.json"),
                                     headers={"Accept": "application/json", "User-Agent": "Vertep-Updater/1"})
    with urllib.request.urlopen(request, timeout=15, context=ssl.create_default_context()) as response:
        if response.status != 200:
            raise RuntimeError(f"Update server returned HTTP {response.status}")
        payload = response.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise RuntimeError("Update manifest is too large")
    manifest = json.loads(payload)
    if not isinstance(manifest, dict):
        raise ValueError("Update manifest must be an object")
    return manifest


def download_package(manifest: dict, destination: Path) -> Path:
    base = update_server_url()
    url = urljoin(base, manifest["package"])
    if urlsplit(url).hostname != urlsplit(base).hostname or urlsplit(url).scheme != "https":
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
