#!/usr/bin/env python3
"""Create the official Ed25519 key that signs module packages of component releases (#510/#511).

The owner runs it once:

    python3 scripts/create-module-signing-key.py | gh secret set MODULE_SIGNING_PRIVATE_KEY --repo fylypovych/vertep

* the private key goes only to stdout (straight into the GitHub secret, never to disk);
* the public key is written to config/module-trust/publishers.json as publisher "vertep"
  for github.com/fylypovych/vertep - commit that file; CORE trusts packages signed by it.

Rotation: run again with --key-id vertep-modules-<n>; the previous public key stays in the file
so packages it signed keep verifying until they are retired.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
TRUST = ROOT / "config" / "module-trust" / "publishers.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key-id", default="vertep-modules-1")
    args = parser.parse_args()
    if sys.stdout.isatty():
        sys.exit("Pipe the output into: gh secret set MODULE_SIGNING_PRIVATE_KEY --repo fylypovych/vertep")
    data = json.loads(TRUST.read_text(encoding="utf-8")) if TRUST.exists() else {}
    publisher = data.setdefault("vertep", {"keys": {}, "repositories": ["github.com/fylypovych/vertep"]})
    if args.key_id in publisher["keys"]:
        sys.exit(f"Key {args.key_id} already exists; choose a new --key-id for a rotation")
    key = Ed25519PrivateKey.generate()
    publisher["keys"][args.key_id] = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    TRUST.parent.mkdir(parents=True, exist_ok=True)
    TRUST.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()).decode("ascii"))
    print(f"Public key {args.key_id} written to {TRUST.relative_to(ROOT)}; commit it.", file=sys.stderr)


if __name__ == "__main__":
    main()
