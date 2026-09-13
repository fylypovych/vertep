import base64
import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


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


def keypair(private_path: Path, public_path: Path) -> None:
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                    "rsa_keygen_bits:2048", "-out", str(private_path)],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private_path), "-pubout",
                    "-out", str(public_path)], check=True, capture_output=True)


def build(module, keys_dir: Path, key_ids: list[str], *, version: int = 1,
          threshold: int = 1, existing=None, revoked=None):
    return module.build_metadata(
        key_ids, keys_dir, threshold, 365, ["stable"], version=version,
        existing_metadata=existing, revoked=set(revoked or ()),
    )


def validate(module, metadata: dict, keys_dir: Path, *, trusted_version: int = 0,
             trusted_sha256=None, now=None):
    return module.validate_root_metadata(
        metadata, keys_dir, trusted_version=trusted_version,
        trusted_sha256=trusted_sha256,
        now=now or datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


@requires_openssl
def test_root_metadata_rotation_revokes_old_key_and_accepts_replacement(tmp_path):
    module = load_module()
    keys = tmp_path / "keys"
    keys.mkdir()
    for key_id in ("root-1", "root-3"):
        keypair(tmp_path / f"{key_id}-private.pem", keys / f"{key_id}.pem")

    initial = build(module, keys, ["root-1"], version=1)
    initial_validated = validate(module, initial, keys)
    rotated = build(
        module, keys, ["root-3"], version=2, existing=initial,
        revoked=["root-1"],
    )
    rotated_validated = validate(
        module, rotated, keys, trusted_version=1,
        trusted_sha256=initial_validated["metadata_sha256"],
    )

    assert rotated_validated["version"] == 2
    assert rotated_validated["release_keys"]["root-1"]["revoked"] is True
    assert rotated_validated["release_keys"]["root-3"]["revoked"] is False
    assert module.authorize_release_key(
        {"key_id": "root-3"}, rotated_validated, keys, "stable"
    ) == keys / "root-3.pem"
    with pytest.raises(RuntimeError, match="revoked"):
        module.authorize_release_key(
            {"key_id": "root-1"}, rotated_validated, keys, "stable"
        )
    with pytest.raises(RuntimeError, match="rollback"):
        validate(module, initial, keys, trusted_version=2)


@requires_openssl
def test_compromised_key_recovery_uses_threshold_rotation(tmp_path):
    module = load_module()
    keys = tmp_path / "keys"
    keys.mkdir()
    for key_id in ("root-a", "root-b", "root-c"):
        keypair(tmp_path / f"{key_id}-private.pem", keys / f"{key_id}.pem")

    initial = build(module, keys, ["root-a", "root-b"], threshold=2, version=1)
    recovered = build(
        module, keys, ["root-b", "root-c"], threshold=2, version=2,
        existing=initial, revoked=["root-a"],
    )
    validated = validate(module, recovered, keys, trusted_version=1)

    assert validated["verified_root_keys"] == ["root-b", "root-c"]
    assert module.authorize_release_key(
        {"key_id": "root-c"}, validated, keys, "stable"
    ) == keys / "root-c.pem"
    with pytest.raises(RuntimeError, match="revoked"):
        module.authorize_release_key(
            {"key_id": "root-a"}, validated, keys, "stable"
        )


@requires_openssl
def test_root_metadata_fail_closed_security_cases(tmp_path):
    module = load_module()
    keys = tmp_path / "keys"
    keys.mkdir()
    keypair(tmp_path / "root-1-private.pem", keys / "root-1.pem")
    metadata = build(module, keys, ["root-1"], version=1)

    expired = copy.deepcopy(metadata)
    expired["expires_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(RuntimeError, match="expired"):
        validate(module, expired, keys)

    revoked_string = copy.deepcopy(metadata)
    revoked_string["release_keys"]["root-1"]["revoked"] = "true"
    with pytest.raises(ValueError, match="revocation state"):
        validate(module, revoked_string, keys)

    tampered_digest = copy.deepcopy(metadata)
    tampered_digest["release_keys"]["root-1"]["sha256"] = "0" * 64
    validated = validate(module, metadata, keys)
    with pytest.raises(RuntimeError, match="digest"):
        module.authorize_release_key(
            {"key_id": "root-1"}, validated, keys, "stable"
        )

    wrong_channel = copy.deepcopy(metadata)
    wrong_channel["release_keys"]["root-1"]["channels"] = ["beta"]
    validated = validate(module, wrong_channel, keys)
    with pytest.raises(RuntimeError, match="channel"):
        module.authorize_release_key(
            {"key_id": "root-1"}, validated, keys, "stable"
        )

    tampered_signature = copy.deepcopy(metadata)
    tampered_signature["signatures"][0]["signature"] = base64.b64encode(b"bad").decode()
    with pytest.raises(RuntimeError, match="threshold"):
        validate(module, tampered_signature, keys)

    with pytest.raises(RuntimeError, match="rollback"):
        validate(module, metadata, keys, trusted_version=2)
    with pytest.raises(RuntimeError, match="equivocates"):
        validate(module, metadata, keys, trusted_version=1, trusted_sha256="1" * 64)


@requires_openssl
def test_root_metadata_cli_reproduces_rotation(tmp_path, monkeypatch):
    module = load_module()
    keys = tmp_path / "keys"
    keys.mkdir()
    for key_id in ("root-1", "root-2"):
        keypair(tmp_path / f"{key_id}-private.pem", keys / f"{key_id}.pem")
    initial_path = tmp_path / "root-metadata-v1.json"
    output_path = tmp_path / "root-metadata-v2.json"
    initial_path.write_text(json.dumps(build(module, keys, ["root-1"], version=1)),
                            encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "generate-root-metadata.py", "--keys-dir", str(keys), "--output", str(output_path),
        "--key-ids", "root-2", "--threshold", "1", "--version", "2",
        "--existing-metadata", str(initial_path), "--revoked", "root-1",
        "--expiry-days", "30", "--channels", "stable",
    ])
    assert module.main() == 0
    rotated = json.loads(output_path.read_text(encoding="utf-8"))
    validated = validate(module, rotated, keys, trusted_version=1)
    assert validated["release_keys"]["root-1"]["revoked"] is True
    assert validated["release_keys"]["root-2"]["revoked"] is False
