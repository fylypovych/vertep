import base64
import hashlib
import subprocess
from datetime import datetime, timezone

import pytest

from core.system_state import (SystemState, dispatch_allowed, get_system_state, jobs_may_be_created,
                               new_job_status, operation_allowed, set_system_state)
from core.update_protocol import (canonical_manifest, validate_manifest, validate_replay_state,
                                  version_tuple)

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


def test_system_state_controls_job_admission(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    set_system_state(SystemState.MAINTENANCE, "draining")
    assert jobs_may_be_created()
    assert new_job_status() == "WAITING_FOR_SYSTEM"
    set_system_state(SystemState.READ_ONLY, "operator lock")
    assert not jobs_may_be_created()
    assert not operation_allowed("recovery")
    set_system_state(SystemState.EMERGENCY, "recovery only")
    assert not jobs_may_be_created()
    assert operation_allowed("recovery")
    assert not operation_allowed("update")


def test_central_state_policy_blocks_mutation_during_update(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    set_system_state(SystemState.UPDATING, "installing")
    assert operation_allowed("read")
    assert operation_allowed("create_job")
    assert not operation_allowed("mutate_job")
    assert not operation_allowed("node_control")
    set_system_state(SystemState.NORMAL, "done")
    assert operation_allowed("configuration")
    set_system_state(SystemState.EMERGENCY, "recovery only")
    assert not jobs_may_be_created()


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


def test_manifest_keyring_channel_and_validity(tmp_path):
    private_key, keyring = tmp_path / "private.pem", tmp_path / "keys"
    keyring.mkdir()
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", str(private_key)], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private_key), "-pubout",
                    "-out", str(keyring / "release-2026.pem")], check=True, capture_output=True)
    manifest = {"version": "1.5.0", "package": "v1/packages/vertep-1.5.0.tar.gz",
                "sha256": hashlib.sha256(b"package").hexdigest(), "channel": "stable",
                "key_id": "release-2026", "issued_at": "2026-08-25T00:00:00Z",
                "expires_at": "2026-09-25T00:00:00Z", "release_sequence": 15}
    message, signature = tmp_path / "manifest", tmp_path / "signature"
    message.write_bytes(canonical_manifest(manifest))
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(signature),
                    str(message)], check=True, capture_output=True)
    manifest["signature"] = base64.b64encode(signature.read_bytes()).decode()
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    assert validate_manifest(manifest, keyring, current="1.4.0", expected_channel="stable", now=now)
    with pytest.raises(RuntimeError, match="channel"):
        validate_manifest(manifest, keyring, current="1.4.0", expected_channel="beta", now=now)
    with pytest.raises(RuntimeError, match="expired"):
        validate_manifest(manifest, keyring, current="1.4.0", expected_channel="stable",
                          now=datetime(2026, 10, 1, tzinfo=timezone.utc))


def test_release_sequence_rejects_replay_and_equivocation():
    accepted = {"release_sequence": 15, "version": "1.5.0", "sha256": "a" * 64,
                "key_id": "release-2026"}
    assert validate_replay_state({**accepted, "signature": "ignored"}, accepted) == accepted
    with pytest.raises(RuntimeError, match="replayed"):
        validate_replay_state({**accepted, "release_sequence": 14}, accepted)
    with pytest.raises(RuntimeError, match="equivocates"):
        validate_replay_state({**accepted, "version": "1.5.1"}, accepted)
    newer = {**accepted, "release_sequence": 16, "version": "1.6.0", "sha256": "b" * 64}
    assert validate_replay_state(newer, accepted) == newer


def test_rolling_compatibility_rejects_contract_migrations(monkeypatch, tmp_path):
    private_key, public_key = tmp_path / "private.pem", tmp_path / "public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", str(private_key)], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)],
                   check=True, capture_output=True)
    manifest = {"version": "1.6.0", "package": "v1/packages/release.tar.gz",
                "sha256": hashlib.sha256(b"package").hexdigest(),
                "database": {"schema": 7, "strategy": "contract", "rollback_safe": False}}
    message, signature = tmp_path / "manifest", tmp_path / "signature"
    message.write_bytes(canonical_manifest(manifest))
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(signature),
                    str(message)], check=True, capture_output=True)
    manifest["signature"] = base64.b64encode(signature.read_bytes()).decode()
    monkeypatch.setenv("REQUIRE_ROLLING_COMPATIBILITY", "true")
    with pytest.raises(RuntimeError, match="Contract migrations"):
        validate_manifest(manifest, public_key, current="1.5.0")
