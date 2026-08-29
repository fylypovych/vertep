import base64
import hashlib
import importlib.util
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.update_lease import UpdateLease
from core.update_trust import (authorize_release_key, canonical_metadata,
                               validate_root_metadata)


requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="integration signature test requires openssl"
)


def _keypair(private_key: Path, public_key: Path) -> None:
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                    "rsa_keygen_bits:2048", "-out", str(private_key)],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private_key), "-pubout",
                    "-out", str(public_key)], check=True, capture_output=True)


def _sign(document: dict, private_key: Path, output: Path) -> str:
    message = output.with_suffix(".json")
    message.write_bytes(canonical_metadata(document))
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key),
                    "-out", str(output), str(message)], check=True, capture_output=True)
    return base64.b64encode(output.read_bytes()).decode()


@requires_openssl
def test_root_metadata_threshold_rotation_and_release_authorization(tmp_path):
    roots, releases = tmp_path / "roots", tmp_path / "releases"
    roots.mkdir()
    releases.mkdir()
    root_private = tmp_path / "root-private.pem"
    release_private = tmp_path / "release-private.pem"
    _keypair(root_private, roots / "root-1.pem")
    _keypair(release_private, releases / "release-2026.pem")
    metadata = {
        "version": 4,
        "expires_at": "2027-01-01T00:00:00Z",
        "threshold": 1,
        "release_keys": {
            "release-2026": {
                "sha256": hashlib.sha256((releases / "release-2026.pem").read_bytes()).hexdigest(),
                "channels": ["stable"],
                "revoked": False,
            }
        },
    }
    metadata["signatures"] = [{
        "key_id": "root-1",
        "signature": _sign(metadata, root_private, tmp_path / "root.sig"),
    }]
    validated = validate_root_metadata(
        metadata, roots, trusted_version=3,
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )
    assert authorize_release_key(
        {"key_id": "release-2026"}, validated, releases, "stable"
    ) == releases / "release-2026.pem"
    with pytest.raises(RuntimeError, match="rollback"):
        validate_root_metadata(metadata, roots, trusted_version=5,
                               now=datetime(2026, 8, 26, tzinfo=timezone.utc))
    with pytest.raises(RuntimeError, match="equivocates"):
        validate_root_metadata(metadata, roots, trusted_version=4,
                               trusted_sha256="0" * 64,
                               now=datetime(2026, 8, 26, tzinfo=timezone.utc))
    validated["release_keys"]["release-2026"]["revoked"] = True
    with pytest.raises(RuntimeError, match="revoked"):
        authorize_release_key({"key_id": "release-2026"}, validated, releases, "stable")


def test_update_lease_fences_a_second_agent(tmp_path):
    with UpdateLease(tmp_path, "first"):
        lock = json.loads((tmp_path / "update.lock").read_text())
        assert lock["operation_id"] == "first"
        with pytest.raises(RuntimeError, match="holds the update lease"):
            with UpdateLease(tmp_path, "second"):
                pass
    with UpdateLease(tmp_path, "third"):
        assert True


def test_audit_log_is_hash_chained(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "update_agent", Path(__file__).parents[1] / "scripts" / "update-agent.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    module.append_audit(tmp_path, {"operation_id": "a", "phase": "CHECKING"})
    module.append_audit(tmp_path, {"operation_id": "a", "phase": "UPDATING"})
    first, second = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert first["previous_hash"] == "0" * 64
    assert second["previous_hash"] == first["event_hash"]
    first["phase"] = "TAMPERED"
    (tmp_path / "audit.jsonl").write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash chain"):
        module.append_audit(tmp_path, {"operation_id": "a", "phase": "NORMAL"})


def test_rollback_contract_verifies_backup_and_restores_database():
    script = (Path(__file__).parents[1] / "scripts" / "vertep").read_text()
    assert "sha256sum -c SHA256SUMS" in script
    assert "database-restore-required" in script
    assert "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" in script
    assert 'release-layout.py" prepare' in script
    assert 'release-layout.py" activate' in script
    assert 'release-layout.py" rollback' in script
    assert 'cp -a "$payload"/. "$ROOT"/' not in script


def test_bootstrap_installs_only_checksum_verified_host_update_executor():
    bootstrap = (Path(__file__).parents[1] / "bootstrap.sh").read_text()
    for artifact in ("update-agent.py", "vertep", "safe-extract.py", "release-layout.py",
                     "vertep-update.service", "vertep-update.path"):
        assert f'.files["{artifact}"].sha256' in bootstrap or 'unit_sha=$(jq' in bootstrap
    assert 'sha256sum -c -' in bootstrap
    assert 'systemctl enable --now vertep-update.path vertep-update.timer' in bootstrap
    assert "SETUP_TOKEN_EXPIRES_AT=" in bootstrap
    assert '.schema == 2 and .product == "vertep"' in bootstrap
    assert '.roles.catalog_sha256' in bootstrap
    assert 'release-sbom.cdx.json' in bootstrap
    assert 'resolved_image(){' in bootstrap
    assert 'monitoring/grafana/dashboards/fleet.json' in bootstrap


def test_bootstrap_preflight_and_text_model_provisioning_contract():
    bootstrap = (Path(__file__).parents[1] / "bootstrap.sh").read_text()
    assert "getent ahosts" in bootstrap
    assert "NTPSynchronized" in bootstrap
    assert "TCP port 8443 is already in use" in bootstrap
    assert "unsupported /opt filesystem" in bootstrap
    deployment = (Path(__file__).parents[1] / "scripts/apply-deployment.py").read_text()
    assert '"ollama", "pull", model' in deployment
    assert "VERTEP_ROLE" in bootstrap
    assert "vertep-deployment.path" in bootstrap
    assert "database migration did not complete successfully" in bootstrap
