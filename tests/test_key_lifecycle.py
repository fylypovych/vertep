import base64
import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.update_trust import authorize_release_key, validate_root_metadata


requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="integration signature test requires openssl"
)


def load_module():
    path = Path(__file__).parents[1] / "scripts" / "generate-root-metadata.py"
    spec = importlib.util.spec_from_file_location("generate_root_metadata", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def public_for(private_path: Path, public_path: Path) -> None:
    """Derive a public key from a private key (used for the verification keyring)."""
    subprocess.run(["openssl", "pkey", "-in", str(private_path), "-pubout",
                    "-out", str(public_path)], check=True, capture_output=True)


def setup_keys(module, tmp_path: Path, key_ids: list[str]):
    """Create a signing key store (private) and a public verification keyring.

    ``generate-root-metadata`` is the signing tool (keys_dir holds private keys);
    ``core/update_trust`` validates and authorizes against PUBLIC keyrings, so the
    tests keep the two stores separate.
    """
    keys_dir = tmp_path / "signing"
    keys_dir.mkdir()
    public_root = tmp_path / "public"
    public_root.mkdir()
    for key_id in key_ids:
        module.generate_key(key_id, keys_dir)
        public_for(keys_dir / f"{key_id}.pem", public_root / f"{key_id}.pem")
    return keys_dir, public_root


def build(module, keys_dir: Path, key_ids: list[str], *, version: int = 1,
          threshold: int = 1, existing=None, revoked=None):
    return module.build_metadata(
        key_ids, keys_dir, threshold, 365, ["stable"], version=version,
        existing_metadata=existing, revoked=set(revoked or ()),
    )


def validate(metadata: dict, public_root: Path, *, trusted_version: int = 0,
             trusted_sha256: str | None = None, now=None):
    return validate_root_metadata(
        metadata, public_root, trusted_version=trusted_version,
        trusted_sha256=trusted_sha256,
        now=now or datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def resign(module, metadata: dict, keys_dir: Path) -> dict:
    """Re-sign canonical messages for every declared key with the private keys.

    Signature verification runs before field normalization, so any tampering of a
    metadata field must be followed by a fresh, valid signature for the security
    checks to reach the intended field-level validation.
    """
    message = module.canonical_metadata(metadata)
    signatures = []
    for item in metadata.get("signatures", []):
        key_path = keys_dir / f"{item['key_id']}.pem"
        signatures.append({"key_id": item["key_id"],
                           "signature": module.sign_metadata(message, key_path)})
    resigned = dict(metadata)
    resigned["signatures"] = signatures
    return resigned


@requires_openssl
def test_root_metadata_rotation_revokes_old_key_and_accepts_replacement(tmp_path):
    module = load_module()
    keys_dir, public_root = setup_keys(module, tmp_path, ["root-1", "root-3"])

    initial = build(module, keys_dir, ["root-1"], version=1)
    initial_validated = validate(initial, public_root)
    rotated = build(
        module, keys_dir, ["root-3"], version=2, existing=initial,
        revoked=["root-1"],
    )
    rotated_validated = validate(
        rotated, public_root, trusted_version=1,
        trusted_sha256=initial_validated["metadata_sha256"],
    )

    assert rotated_validated["version"] == 2
    assert rotated_validated["release_keys"]["root-1"]["revoked"] is True
    assert rotated_validated["release_keys"]["root-3"]["revoked"] is False
    assert authorize_release_key(
        {"key_id": "root-3"}, rotated_validated, public_root, "stable"
    ) == public_root / "root-3.pem"
    with pytest.raises(RuntimeError, match="revoked"):
        authorize_release_key(
            {"key_id": "root-1"}, rotated_validated, public_root, "stable"
        )
    with pytest.raises(RuntimeError, match="rollback"):
        validate(initial, public_root, trusted_version=2)


@requires_openssl
def test_compromised_key_recovery_uses_threshold_rotation(tmp_path):
    module = load_module()
    keys_dir, public_root = setup_keys(module, tmp_path, ["root-a", "root-b", "root-c"])

    initial = build(module, keys_dir, ["root-a", "root-b"], threshold=2, version=1)
    recovered = build(
        module, keys_dir, ["root-b", "root-c"], threshold=2, version=2,
        existing=initial, revoked=["root-a"],
    )
    validated = validate(recovered, public_root, trusted_version=1)

    assert validated["verified_root_keys"] == ["root-b", "root-c"]
    assert authorize_release_key(
        {"key_id": "root-c"}, validated, public_root, "stable"
    ) == public_root / "root-c.pem"
    with pytest.raises(RuntimeError, match="revoked"):
        authorize_release_key(
            {"key_id": "root-a"}, validated, public_root, "stable"
        )


@requires_openssl
def test_root_metadata_fail_closed_security_cases(tmp_path):
    module = load_module()
    keys_dir, public_root = setup_keys(module, tmp_path, ["root-1"])
    metadata = build(module, keys_dir, ["root-1"], version=1)

    expired = copy.deepcopy(metadata)
    expired["expires_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(RuntimeError, match="expired"):
        validate(expired, public_root)

    revoked_string = resign(module, dict(metadata), keys_dir)
    revoked_string["release_keys"]["root-1"]["revoked"] = "true"
    revoked_string = resign(module, revoked_string, keys_dir)
    with pytest.raises(ValueError, match="revocation state"):
        validate(revoked_string, public_root)

    validated = validate(metadata, public_root)
    tampered_digest = copy.deepcopy(validated)
    tampered_digest["release_keys"]["root-1"]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="digest"):
        authorize_release_key(
            {"key_id": "root-1"}, tampered_digest, public_root, "stable"
        )

    wrong_channel = resign(module, dict(metadata), keys_dir)
    wrong_channel["release_keys"]["root-1"]["channels"] = ["beta"]
    wrong_channel = resign(module, wrong_channel, keys_dir)
    wrong_validated = validate(wrong_channel, public_root)
    with pytest.raises(RuntimeError, match="channel"):
        authorize_release_key(
            {"key_id": "root-1"}, wrong_validated, public_root, "stable"
        )

    tampered_signature = copy.deepcopy(metadata)
    tampered_signature["signatures"][0]["signature"] = base64.b64encode(b"bad").decode()
    with pytest.raises(RuntimeError, match="threshold"):
        validate(tampered_signature, public_root)

    with pytest.raises(RuntimeError, match="rollback"):
        validate(metadata, public_root, trusted_version=2)
    with pytest.raises(RuntimeError, match="equivocates"):
        validate(metadata, public_root, trusted_version=1, trusted_sha256="1" * 64)


@requires_openssl
def test_root_metadata_cli_reproduces_rotation(tmp_path, monkeypatch):
    module = load_module()
    keys_dir, public_root = setup_keys(module, tmp_path, ["root-1", "root-2"])
    initial_path = tmp_path / "root-metadata-v1.json"
    output_path = tmp_path / "root-metadata-v2.json"
    initial_path.write_text(json.dumps(build(module, keys_dir, ["root-1"], version=1)),
                            encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "generate-root-metadata.py", "--keys-dir", str(keys_dir), "--output", str(output_path),
        "--key-ids", "root-2", "--threshold", "1", "--version", "2",
        "--existing-metadata", str(initial_path), "--revoked", "root-1",
        "--expiry-days", "30", "--channels", "stable",
    ])
    assert module.main() == 0
    rotated = json.loads(output_path.read_text(encoding="utf-8"))
    validated = validate(rotated, public_root, trusted_version=1)
    assert validated["release_keys"]["root-1"]["revoked"] is True
    assert validated["release_keys"]["root-2"]["revoked"] is False