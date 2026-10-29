"""Controlled signed reviewer/stand responses for governance contract tests only.

These fixtures never qualify a physical stand or close an actual GitHub issue.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat, load_pem_private_key
from lab.evidence import evidence_digest


def signed_contract_evidence(evidence, *, root=None, trust=None):
    root = Path(root or os.environ["LAB_EVIDENCE_ROOT"])
    trust = Path(trust or os.environ["LAB_REVIEW_TRUST_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    trust.mkdir(parents=True, exist_ok=True)
    evidence = dict(evidence)
    evidence["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    report = {key: evidence[key] for key in ("commit", "environment", "expected", "actual", "test_results")}
    report.update(evidence_kind="real_server", simulated=False)
    path = root / "controlled-stand-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    evidence["artifact"] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    name = evidence["reviewer"]
    private_path = root / f"test-reviewer-{name}.key"
    if private_path.exists():
        key = load_pem_private_key(private_path.read_bytes(), password=None)
    else:
        key = Ed25519PrivateKey.generate()
        private_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    (trust / f"{name}.pem").write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    digest = evidence_digest(evidence)
    evidence["review_attestation"] = {"reviewer": name, "digest": digest, "signature": base64.b64encode(key.sign(digest.encode())).decode()}
    return evidence
