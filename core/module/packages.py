"""Verify external module packages before persisting or loading any code."""

import base64
import json
import re
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .manifest import ModuleManifest
from .registry import ModuleRegistry


class ModulePackages:
    def __init__(self, root: Path, trusted_keys: dict[str, bytes], system_versions=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.keys = trusted_keys
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
        """Signature covers exact canonical manifest and every declared file byte."""
        content = {"manifest": package["manifest"], "files": package["files"]}
        raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        key = load_pem_public_key(self.keys[package["signer"]])
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("Module signer must use Ed25519")
        key.verify(base64.b64decode(package["signature"], validate=True), raw)
        for name, encoded in content["files"].items():
            # Paths remain logical package names: no archive extraction or import
            # happens during verification/discovery.
            if not name or "\\" in name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")) or ":" in name:
                raise ValueError("Unsafe module file path")
            base64.b64decode(encoded, validate=True)
        manifest = ModuleManifest.from_dict(content["manifest"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", manifest.name):
            raise ValueError("Unsafe module name")
        return manifest

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
