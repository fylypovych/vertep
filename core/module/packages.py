"""Verify external module packages before persisting or loading any code."""

import base64
import json
import re
from pathlib import Path

from .manifest import ModuleManifest
from .registry import ModuleRegistry
from .trust import TrustError, TrustStore


class ModulePackages:
    def __init__(self, root: Path, trusted_keys: "dict[str, bytes] | TrustStore", system_versions=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.trust = trusted_keys if isinstance(trusted_keys, TrustStore) else TrustStore.from_legacy_keys(trusted_keys)
        if system_versions is None:
            system_versions = {"core_runtime": (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()}
        self.registry = ModuleRegistry(system_versions)
        self._records = {}
        pending = {}
        for path in self.root.glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            self._records[record["name"]] = record
            if record["enabled"]:
                pending[record["name"]] = self.verify(record["package"])
        while pending:
            loaded = [name for name, manifest in pending.items() if self.registry.register_module(manifest)]
            if not loaded:
                raise ValueError("Installed module dependencies are incompatible or cyclic")
            for name in loaded:
                pending.pop(name)

    def verify(self, package: dict) -> ModuleManifest:
        return verify_package(package, self.trust)

    def _save(self, record):
        path = self.root / f"{record['name']}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def install(self, package: dict):
        manifest = self.verify(package)
        if manifest.name in self._records and self._records[manifest.name]["enabled"]:
            raise ValueError("Module already installed; unload before replacement")
        if not self.registry.register_module(manifest):
            raise ValueError("Module dependencies are not satisfied")
        record = {"name": manifest.name, "enabled": True, "package": package}
        try:
            self._save(record)
        except OSError:
            self.registry.unregister_module(manifest.name)
            raise
        self._records[manifest.name] = record
        return manifest.to_dict()

    def uninstall(self, name):
        previous = self.registry.get_module(name)
        if not self.registry.unregister_module(name):
            raise ValueError("Module missing or required by another module")
        record = {**self._records[name], "enabled": False}
        try:
            self._save(record)
        except OSError:
            self.registry.register_module(previous, force=True)
            raise
        self._records[name] = record
        # Retain signed package for rollback/audit; no filesystem deletion.

    def list(self):
        return [manifest.to_dict() for manifest in self.registry.list_modules()]

    def info(self, name):
        manifest = self.registry.get_module(name)
        if manifest is None:
            raise ValueError("Module is not installed")
        return manifest.to_dict()


def verify_package(package: dict, trust: TrustStore) -> ModuleManifest:
    """Signature covers exact canonical manifest and every declared file byte.

    Then the signing key's publisher must be the manifest issuer and the
    provenance repository must be trusted for it (forks are not inherited).
    """
    from cryptography.exceptions import InvalidSignature
    content = {"manifest": package["manifest"], "files": package["files"]}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    publisher, key = trust.key(package.get("signer", ""))
    try:
        key.verify(base64.b64decode(package["signature"], validate=True), raw)
    except InvalidSignature as error:
        raise TrustError("Module package signature or content is invalid") from error
    for name, encoded in content["files"].items():
        # Paths remain logical package names: no archive extraction or import
        # happens during verification/discovery.
        if not name or "\\" in name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")) or ":" in name:
            raise ValueError("Unsafe module file path")
        base64.b64decode(encoded, validate=True)
    manifest = ModuleManifest.from_dict(content["manifest"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", manifest.name):
        raise ValueError("Unsafe module name")
    trust.check_provenance(publisher, manifest)
    if manifest.ui and manifest.ui["files"]:
        import hashlib
        for path, digest in manifest.ui["files"].items():
            # Signed digests bind the UI bundle bytes shipped in this package.
            if path not in content["files"] or hashlib.sha256(base64.b64decode(content["files"][path])).hexdigest() != digest:
                raise TrustError("Module UI bundle does not match its signed digest")
    return manifest
