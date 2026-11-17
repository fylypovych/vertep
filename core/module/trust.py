"""Publisher trust and provenance for signed module packages.

Trust root layout (``config/module-trust`` / ``VERTEP_MODULE_TRUST_ROOT``):

* ``publishers.json`` - ``{"<publisher>": {"keys": {"<key_id>": "<PEM>"},
  "repositories": ["github.com/org/repo"]}}``. Schema 2.x packages must name
  this publisher, be signed by one of its keys (issuer check) and come from one
  of its repositories.
* ``<key_id>.pem`` - legacy single keys. They verify only legacy schema 1.x
  packages, exactly as before.

A fork is a different repository. It is never trusted through the upstream
publisher: the administrator must add the fork's own publisher record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key


class TrustError(ValueError):
    """Package trust/provenance rejected."""


@dataclass
class Publisher:
    publisher_id: str
    keys: dict[str, bytes]
    repositories: list[str] = field(default_factory=list)
    legacy: bool = False


class TrustStore:
    def __init__(self, publishers: dict[str, Publisher] | None = None) -> None:
        self.publishers = dict(publishers or {})
        owners: dict[str, str] = {}
        for publisher in self.publishers.values():
            for key_id in publisher.keys:
                if key_id in owners and owners[key_id] != publisher.publisher_id:
                    raise TrustError(f"Signing key id {key_id} is bound to two publishers")
                owners[key_id] = publisher.publisher_id
        self._owners = owners

    @classmethod
    def from_legacy_keys(cls, keys: dict[str, bytes]) -> "TrustStore":
        return cls({name: Publisher(name, {name: pem}, legacy=True) for name, pem in keys.items()})

    @classmethod
    def from_root(cls, root: Path) -> "TrustStore":
        root = Path(root)
        publishers = {path.stem: Publisher(path.stem, {path.stem: path.read_bytes()}, legacy=True)
                      for path in sorted(root.glob("*.pem"))}
        registry = root / "publishers.json"
        if registry.exists():
            data = json.loads(registry.read_text(encoding="utf-8"))
            for publisher_id, record in data.items():
                if publisher_id in publishers:
                    raise TrustError(f"Publisher {publisher_id} is declared twice")
                keys = record.get("keys") or {}
                repositories = record.get("repositories") or []
                if not keys or not isinstance(keys, dict) or not isinstance(repositories, list):
                    raise TrustError(f"Publisher {publisher_id} needs keys and repositories")
                publishers[publisher_id] = Publisher(publisher_id, {k: v.encode() for k, v in keys.items()},
                                                     list(repositories))
        return cls(publishers)

    def key(self, signer: str) -> tuple[Publisher, Ed25519PublicKey]:
        owner = self._owners.get(signer)
        if owner is None:
            raise TrustError("Package signer is not trusted")
        key = load_pem_public_key(self.publishers[owner].keys[signer])
        if not isinstance(key, Ed25519PublicKey):
            raise TrustError("Module signer must use Ed25519")
        return self.publishers[owner], key

    def check_provenance(self, publisher: Publisher, manifest) -> None:
        """Issuer and repository binding. Raises TrustError."""
        if publisher.legacy:
            if manifest.schema_version.split(".")[0] != "1":
                raise TrustError("Legacy key may sign only schema 1.x packages; register the publisher")
            return
        if manifest.publisher != publisher.publisher_id:
            raise TrustError("Package publisher does not match the signing key issuer")
        repository = (manifest.provenance or {}).get("repository")
        if not repository:
            raise TrustError("Package provenance repository is required")
        if repository not in publisher.repositories:
            # Includes forks of a trusted repository: upstream trust is not inherited.
            raise TrustError("Package repository is not trusted for this publisher (forks need their own trust)")
