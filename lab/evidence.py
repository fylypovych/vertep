"""Verify exact source, machine evidence and an owner-trusted independent attestation."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def evidence_digest(evidence):
    body = {key: value for key, value in evidence.items() if key != "review_attestation"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def verify(evidence, actor, issue, state_root):
    problems = []
    if not re.fullmatch(r"[0-9a-fA-F]{40}", str(evidence.get("commit", ""))):
        problems.append("evidence requires a full commit SHA")
    if str(issue).startswith("ir.") and evidence.get("identifier") != issue:
        problems.append("evidence targets another issue")
    try:
        artifact = evidence["artifact"]
        path = Path(artifact["path"]).resolve()
        root = Path(os.getenv("LAB_EVIDENCE_ROOT", str(state_root))).resolve()
        if not path.is_relative_to(root):
            raise ValueError("artifact escapes the configured evidence root")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
            raise ValueError("artifact checksum mismatch")
        report = json.loads(raw)
        for key in ("commit", "environment", "expected", "actual", "test_results"):
            if report.get(key) != evidence.get(key):
                raise ValueError(f"artifact {key} differs from submitted evidence")
        if report.get("evidence_kind") != "real_server" or report.get("simulated") is not False:
            raise ValueError("real acceptance requires non-simulated real-server evidence")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        problems.append(f"invalid machine evidence: {exc}")
    try:
        attestation = evidence["review_attestation"]
        reviewer = str(evidence["reviewer"])
        if reviewer == actor or not re.fullmatch(r"[A-Za-z0-9_-]+", reviewer):
            raise ValueError("invalid independent reviewer identity")
        if attestation["reviewer"] != reviewer or attestation["digest"] != evidence_digest(evidence):
            raise ValueError("review scope mismatch")
        trust = os.environ.get("LAB_REVIEW_TRUST_ROOT")
        if not trust:
            raise ValueError("review trust root is not configured")
        key = load_pem_public_key((Path(trust) / f"{reviewer}.pem").read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("review must use an owner-trusted Ed25519 key")
        key.verify(base64.b64decode(attestation["signature"], validate=True), attestation["digest"].encode())
    except Exception as exc:
        problems.append(f"unconfirmed independent review: {type(exc).__name__}: {exc}")
    return problems
