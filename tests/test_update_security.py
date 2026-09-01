import base64
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace
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


def test_update_lease_uses_postgres_epoch_fence(monkeypatch, tmp_path):
    calls = []

    class Cursor:
        def __init__(self, row=None):
            self.row = row
        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, query, params=()):
            calls.append((" ".join(query.split()), params))
            if "pg_try_advisory_lock" in query:
                return Cursor((True,))
            if "RETURNING epoch" in query:
                return Cursor((7,))
            return Cursor()
        def close(self):
            calls.append(("close", ()))

    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(
        connect=lambda *args, **kwargs: Connection()))
    with UpdateLease(tmp_path, "distributed") as lease:
        assert lease.fence_epoch == 7
        assert json.loads((tmp_path / "update.lock").read_text())["fence_epoch"] == 7
    assert any("pg_advisory_unlock" in query for query, _ in calls)


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
    assert 'systemctl enable vertep-startup-recovery.service' in bootstrap
    assert 'vertep-watchdog.timer vertep-startup-recovery.service' not in bootstrap
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
    assert '"${compose[@]}" wait migrate' not in bootstrap
    assert 'progress "Waiting for runtime health checks"' in bootstrap
    assert 'next_health_report=$((SECONDS+30))' in bootstrap
    assert "'.[$role].services[]'" in bootstrap
    assert "'.roles.profiles[$role].services[]'" not in bootstrap
    assert 'has($role)' in bootstrap
    assert "^[0-9a-f]{12}_vertep-" in bootstrap
    assert 'docker rm -f "$stale_id"' in bootstrap
    recovery_unit = (Path(__file__).parents[1] / "installer/vertep-startup-recovery.service").read_text()
    assert "[Install]" in recovery_unit
    assert "WantedBy=multi-user.target" in recovery_unit


def test_bootstrap_resume_preserves_installation_identity_and_mutable_state():
    bootstrap = (Path(__file__).parents[1] / "bootstrap.sh").read_text()
    assert 'existing_secret "$INSTALL_ROOT/config/postgres.password" POSTGRES_PASSWORD' in bootstrap
    assert 'existing_secret "$INSTALL_ROOT/config/redis.password" REDIS_PASSWORD' in bootstrap
    assert 'existing_secret "$INSTALL_ROOT/config/secret-store.passphrase"' in bootstrap
    assert '[[ -s "$INSTALL_ROOT/config/postgres.password" ]]' in bootstrap
    assert '[[ ! -f "$INSTALL_ROOT/config/secrets.enc.json"' in bootstrap
    assert '[[ ! -f "$INSTALL_ROOT/config/deployment-plan.json" ]]' in bootstrap
    assert 'existing TLS certificate/key pair is incomplete' in bootstrap
    assert 'existing node CA certificate/key pair is incomplete' in bootstrap
    assert 'chmod -R u+rX,g+rX,o-rwx "$bundle_root"' in bootstrap
    assert 'chmod -R u+rX,g+rX,o-rwx "$INSTALL_ROOT"' not in bootstrap
    assert 'docker volume inspect vertep_postgres-data' in bootstrap
    assert 'existing PostgreSQL data volume found but its password is unavailable' in bootstrap


def test_bootstrap_resume_updates_managed_env_and_preserves_unknown_settings():
    bootstrap = (Path(__file__).parents[1] / "bootstrap.sh").read_text()
    assert 'existing_env_tmp=$(mktemp)' in bootstrap
    assert 'managed_keys = {line.split("=", 1)[0]' in bootstrap
    assert 'line.split("=", 1)[0] in managed_keys' in bootstrap
    assert 'mv -f "$env_tmp" "$INSTALL_ROOT/.env"' in bootstrap
    assert 'existing_node_role=$(env_value NODE_ROLE)' in bootstrap
    assert 'existing_web_domain=$(env_value WEB_DOMAIN)' in bootstrap
    assert 'installation_complete=true' in bootstrap
    assert 'ln -sfn "$INSTALL_ROOT/scripts/vertep" /usr/local/bin/vertep' in bootstrap
    assert "'.version = $version | .runtime = $runtime[0]'" in bootstrap


def test_signed_update_switches_runtime_images_and_host_executors():
    command = (Path(__file__).parents[1] / "scripts/vertep").read_text()
    runtime = (Path(__file__).parents[1] / "scripts/build-runtime-bundle.py").read_text()
    workflow = (Path(__file__).parents[1] / ".github/workflows/release.yml").read_text()
    assert 'python3 "$release_root/scripts/update-runtime-env.py"' in command
    assert '"$ROOT/.env" "$release_root/manifest.json" "$target_version"' in command
    assert '[[ ! -f "$backup/.env" ]] || cp -a "$backup/.env" "$ROOT/.env"' in command
    assert 'sync_host_runtime "$release_root"' in command
    assert "X-Vertep-Internal-Key" in command
    assert "cleanup_stale_compose_replacements" in command
    assert command.index("cleanup_stale_compose_replacements", command.index("apply-update)")) < command.index('"${compose[@]}" up -d postgres redis')
    assert command.count('source "$ROOT/.env"') >= 3
    assert '--exclude=releases' in command and '--exclude=current' in command
    assert '"worker_update.py", "update-runtime-env.py"' in runtime
    assert "scripts/build-update-manifest.py" in workflow
    assert '"update-manifest-$version.json"' in workflow
