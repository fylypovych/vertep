"""Підписаний контракт appliance-релізу Vertep."""

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2,3}")
CHANNEL_RE = re.compile(r"[a-z0-9-]{1,32}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def canonical_contract(contract: dict) -> bytes:
    unsigned = {key: value for key, value in contract.items() if key != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _artifact_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{field} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must stay inside the release bundle")
    return value


def validate_release_contract(contract: dict, artifact_root: Path | None = None,
                              public_key: Path | None = None,
                              now: datetime | None = None) -> dict:
    """Validate structure, inventory, local artifacts and optional RSA signature."""
    if not isinstance(contract, dict) or contract.get("schema") != 2:
        raise ValueError("Release contract schema must be 2")
    if contract.get("product") != "vertep":
        raise ValueError("Release contract product must be vertep")
    version = contract.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError("Release contract version is invalid")
    sequence = contract.get("release_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError("Release sequence must be a positive integer")
    channel = contract.get("channel")
    if not isinstance(channel, str) or not CHANNEL_RE.fullmatch(channel):
        raise ValueError("Release channel is invalid")
    issued_at = _timestamp(contract.get("issued_at"), "issued_at")
    expires_at = _timestamp(contract.get("expires_at"), "expires_at")
    if expires_at <= issued_at:
        raise ValueError("Release contract expiry must be later than issue time")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if issued_at > current_time or expires_at <= current_time:
        raise RuntimeError("Release contract is outside its validity window")

    compatibility = contract.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ValueError("Release compatibility metadata is required")
    for field in ("core_api", "worker_api", "database_schema"):
        value = compatibility.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"Compatibility field {field} must be a positive integer")
    minimum = compatibility.get("minimum_version")
    if not isinstance(minimum, str) or not VERSION_RE.fullmatch(minimum):
        raise ValueError("Compatibility minimum_version is invalid")
    if compatibility.get("database_strategy") not in {"none", "expand", "contract"}:
        raise ValueError("Compatibility database_strategy is invalid")
    if not isinstance(compatibility.get("rollback_safe"), bool):
        raise ValueError("Compatibility rollback_safe must be boolean")

    files = contract.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Release contract has no files")
    normalized_files: dict[str, dict] = {}
    for name, metadata in files.items():
        safe_name = _artifact_path(name, "file name")
        if not isinstance(metadata, dict) or not SHA256_RE.fullmatch(str(metadata.get("sha256", ""))):
            raise ValueError(f"File digest is invalid: {safe_name}")
        size = metadata.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"File size is invalid: {safe_name}")
        normalized_files[safe_name] = {"sha256": metadata["sha256"], "size": size}
        if artifact_root is not None:
            path = artifact_root / safe_name
            if not path.is_file() or path.stat().st_size != size:
                raise RuntimeError(f"Release artifact is missing or has the wrong size: {safe_name}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != metadata["sha256"]:
                raise RuntimeError(f"Release artifact digest mismatch: {safe_name}")

    images = contract.get("images")
    if not isinstance(images, dict) or not images:
        raise ValueError("Release contract has no container images")
    for service, image in images.items():
        if not isinstance(service, str) or not NAME_RE.fullmatch(service) or not isinstance(image, dict):
            raise ValueError("Release contract contains an invalid image entry")
        reference, digest = image.get("reference"), image.get("digest")
        platforms = image.get("platforms")
        if not isinstance(reference, str) or not reference or "@" in reference:
            raise ValueError(f"Image reference is invalid: {service}")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise ValueError(f"Image digest is invalid: {service}")
        if not isinstance(platforms, list) or not platforms or any(
                platform not in {"linux/amd64", "linux/arm64"} for platform in platforms):
            raise ValueError(f"Image platforms are invalid: {service}")

    roles = contract.get("roles")
    if not isinstance(roles, dict):
        raise ValueError("Release role inventory is required")
    catalog_file = _artifact_path(roles.get("catalog_file"), "role catalog file")
    if catalog_file not in normalized_files or roles.get("catalog_sha256") != normalized_files[catalog_file]["sha256"]:
        raise ValueError("Role catalog digest is not bound to the file inventory")
    profiles = roles.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("Release contract has no role profiles")
    for role, profile in profiles.items():
        if not isinstance(role, str) or not NAME_RE.fullmatch(role) or not isinstance(profile, dict):
            raise ValueError("Release contract contains an invalid role")
        services = profile.get("services")
        if not isinstance(services, list) or not services or any(service not in images for service in services):
            raise ValueError(f"Role references a service without a pinned image: {role}")
        for field in ("capabilities", "modules"):
            values = profile.get(field)
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"Role {field} is invalid: {role}")
    if artifact_root is not None:
        try:
            catalog = json.loads((artifact_root / catalog_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("Role catalog is unreadable") from error
        if not isinstance(catalog, dict) or set(catalog) != set(profiles):
            raise RuntimeError("Signed role inventory does not match the role catalog")
        for role, definition in catalog.items():
            expected = {field: definition.get(field, [])
                        for field in ("services", "capabilities", "modules")}
            if profiles[role] != expected:
                raise RuntimeError(f"Signed role profile does not match the role catalog: {role}")

    sbom = contract.get("sbom")
    if not isinstance(sbom, dict):
        raise ValueError("Release SBOM metadata is required")
    sbom_file = _artifact_path(sbom.get("file"), "SBOM file")
    if (sbom.get("format") != "CycloneDX-JSON" or sbom_file not in normalized_files
            or sbom.get("sha256") != normalized_files[sbom_file]["sha256"]):
        raise ValueError("Release SBOM is not bound to the file inventory")
    if artifact_root is not None:
        try:
            sbom_document = json.loads((artifact_root / sbom_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("Release SBOM is unreadable") from error
        if sbom_document.get("bomFormat") != "CycloneDX" or not sbom_document.get("specVersion"):
            raise RuntimeError("Release SBOM is not a CycloneDX document")

    if public_key is not None:
        try:
            signature = base64.b64decode(contract.get("signature", ""), validate=True)
            key = serialization.load_pem_public_key(public_key.read_bytes())
            if not isinstance(key, rsa.RSAPublicKey):
                raise ValueError("Release verification key must be RSA")
            key.verify(signature, canonical_contract(contract), padding.PKCS1v15(), hashes.SHA256())
        except (OSError, TypeError, ValueError, InvalidSignature) as error:
            raise RuntimeError("Release contract signature verification failed") from error
    return contract


def sign_release_contract(contract: dict, private_key: Path) -> dict:
    key = serialization.load_pem_private_key(private_key.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("Release signing key must be RSA")
    signed = {key: value for key, value in contract.items() if key != "signature"}
    signature = key.sign(canonical_contract(signed), padding.PKCS1v15(), hashes.SHA256())
    signed["signature"] = base64.b64encode(signature).decode("ascii")
    return signed
