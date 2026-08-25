import base64
import hashlib
import subprocess

import pytest

from core.system_state import SystemState, dispatch_allowed, get_system_state, set_system_state
from core.update_protocol import canonical_manifest, validate_manifest, version_tuple


def test_global_state_is_durable_and_blocks_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    assert dispatch_allowed()
    set_system_state(SystemState.MAINTENANCE, "draining", "operation-1")
    assert get_system_state()["operation_id"] == "operation-1"
    assert not dispatch_allowed()
    set_system_state(SystemState.NORMAL, "complete")
    assert dispatch_allowed()


def test_signed_manifest_and_compatibility(tmp_path):
    private_key, public_key = tmp_path / "private.pem", tmp_path / "public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", str(private_key)], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)],
                   check=True, capture_output=True)
    manifest = {"version": "1.5.0", "min_version": "1.0.0", "max_version": "1.4.9",
                "required": False, "package": "v1/packages/vertep-1.5.0.tar.gz",
                "sha256": hashlib.sha256(b"package").hexdigest()}
    message, signature = tmp_path / "manifest", tmp_path / "signature"
    message.write_bytes(canonical_manifest(manifest))
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(signature),
                    str(message)], check=True, capture_output=True)
    manifest["signature"] = base64.b64encode(signature.read_bytes()).decode()
    assert validate_manifest(manifest, public_key, current="1.4.0")["version"] == "1.5.0"
    with pytest.raises(RuntimeError, match="newer"):
        validate_manifest(manifest, public_key, current="2.0.0")


def test_versions_are_strictly_numeric():
    assert version_tuple("0.0.0.12") == (0, 0, 0, 12)
    with pytest.raises(ValueError):
        version_tuple("1.2.3-rc1")
