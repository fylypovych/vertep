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
    script = (Path(__file__).parents[1] / "scripts" / "vertep").read_text(encoding="utf-8")
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
    assert "python3-psycopg" in bootstrap
    assert "Pending update superseded by bootstrap" in bootstrap


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
    assert 'required_services_healthy(){' in bootstrap
    assert 'for service in "${role_services[@]}"' in bootstrap
    assert '[[ $service == migrate ]] && continue' in bootstrap
    assert 'ps --services --filter status=running | wc -l' not in bootstrap
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
    command = (Path(__file__).parents[1] / "scripts/vertep").read_text(encoding="utf-8")
    runtime = (Path(__file__).parents[1] / "scripts/build-runtime-bundle.py").read_text()
    workflow = (Path(__file__).parents[1] / ".github/workflows/release.yml").read_text()
    assert 'python3 "$release_root/scripts/update-runtime-env.py"' in command
    assert '"$ROOT/.env" "$release_root/manifest.json" "$target_version"' in command
    assert '[[ ! -f "$backup/.env" ]] || cp -a "$backup/.env" "$ROOT/.env"' in command
    assert 'sync_host_runtime "$release_root"' in command
    assert '[[ ! "$release_root/config/node_roles.json" -ef "$ROOT/config/node_roles.json" ]]' in command
    assert 'ln -sfn "$ROOT/scripts/vertep" /usr/local/bin/vertep' not in command
    assert "X-Vertep-Internal-Key" in command
    assert "cleanup_stale_compose_replacements" in command
    assert command.index("cleanup_stale_compose_replacements", command.index("apply-update)")) < command.index('missing_infra=()')
    assert 'up -d --force-recreate --no-deps --remove-orphans' in command
    assert command.count('source "$ROOT/.env"') >= 3
    assert '--exclude=releases' in command and '--exclude=current' in command
    assert '"worker_update.py", "update-runtime-env.py"' in runtime
    assert "scripts/build-update-manifest.py" in workflow
    assert '"update-manifest-$version.json"' in workflow


def test_update_service_can_replace_only_managed_unit_files():
    unit = (Path(__file__).parents[1] / "installer" / "vertep-update.service").read_text()
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=@VERTEP_ROOT@ /etc/systemd/system" in unit
    assert "/usr/local" not in unit

# ── recover_if_interrupted: unreadable status.json + evidence detection ────


def _load_agent():
    spec = importlib.util.spec_from_file_location(
        "update_agent", Path(__file__).parents[1] / "scripts" / "update-agent.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_has_interrupt_evidence_returns_false_when_empty(tmp_path):
    """No audit log, no requests directory -> no evidence."""
    agent = _load_agent()
    assert agent._has_interrupt_evidence(tmp_path) is False


def test_has_interrupt_evidence_returns_true_for_audit_log(tmp_path):
    """Non-empty audit.jsonl is durable evidence of a mid-flight update."""
    agent = _load_agent()
    (tmp_path / "audit.jsonl").write_text(
        '{"operation_id":"a","phase":"CHECKING","event_hash":"x","previous_hash":"0000"}\n',
        encoding="utf-8")
    assert agent._has_interrupt_evidence(tmp_path) is True


def test_has_interrupt_evidence_returns_true_for_pending_requests(tmp_path):
    """A pending request file is durable evidence."""
    agent = _load_agent()
    requests_dir = tmp_path / "requests"
    requests_dir.mkdir()
    (requests_dir / "req-abc.json").write_text('{"action":"update"}', encoding="utf-8")
    assert agent._has_interrupt_evidence(tmp_path) is True


def test_has_interrupt_evidence_handles_unreadable_audit(tmp_path):
    """Corrupted/unreadable audit.jsonl should not cause a crash."""
    agent = _load_agent()
    audit = tmp_path / "audit.jsonl"
    audit.write_bytes(b"\xff\xfe\x00\x00\x80")  # noqa: use raw bytes, not encoding
    assert agent._has_interrupt_evidence(tmp_path) is False


def test_recover_if_interrupted_unreadable_status_with_evidence(tmp_path, monkeypatch):
    """When status.json is unreadable but audit.jsonl has entries, recovery triggers."""
    agent = _load_agent()
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    agent.append_audit(tmp_path, {"operation_id": "test", "phase": "UPDATING"})
    (tmp_path / "status.json").write_bytes(b"{truncated")
    with pytest.raises(Exception):
        agent.recover_if_interrupted(Path("/nonexistent"), tmp_path)
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["phase"] == "RECOVERING"
    assert "unreadable" in status["message"]


def test_recover_if_interrupted_unreadable_status_no_evidence(tmp_path, monkeypatch):
    """When status.json is unreadable AND there's no evidence, stay silent."""
    agent = _load_agent()
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    (tmp_path / "status.json").write_bytes(b"not json")
    agent.recover_if_interrupted(Path("/nonexistent"), tmp_path)
    assert (tmp_path / "status.json").read_bytes() == b"not json"


# ── Worker restart consumer trigger ────────────────────────────────────────


def test_worker_restart_triggers_local_update(tmp_path, monkeypatch):
    """When CORE sets desired_state=RESTARTING, Worker must call request_local_update(action='restart')."""
    source = (Path(__file__).parents[1] / "worker" / "service.py").read_text(encoding="utf-8")
    assert 'request_local_update(update_target or "current", action="restart",' in source
    assert "request_id=restart_operation_id" in source


def test_worker_restart_action_matches_update_agent_handler():
    """update-agent.py must handle action='restart' with restart-runtime."""
    agent_source = (Path(__file__).parents[1] / "scripts" / "update-agent.py").read_text(encoding="utf-8")
    assert '"restart"' in agent_source
    assert "restart-runtime" in agent_source
    assert 'action == "restart"' in agent_source
    assert 'action == "rollback"' in agent_source


# ── Fault-injection: concurrent update requests ────────────────────────────


def test_update_lease_prevents_concurrent_agents(tmp_path):
    """Two UpdateLease contexts cannot overlap for the same state dir."""
    with UpdateLease(tmp_path, "agent-1"):
        with pytest.raises(RuntimeError, match="holds the update lease"):
            with UpdateLease(tmp_path, "agent-2"):
                pass
    # After first releases, third can acquire
    with UpdateLease(tmp_path, "agent-3"):
        pass


def test_update_lease_stale_lock_acquired_after_timeout(tmp_path):
    """A stale lock with expired TTL can be forcefully acquired."""
    import time as _time
    lock_path = tmp_path / "update.lock"
    stale_lock = {"operation_id": "stale-agent", "acquired_at": (_time.time() - 7200),
                  "ttl_seconds": 3600, "fence_epoch": 0}
    lock_path.write_text(json.dumps(stale_lock), encoding="utf-8")
    with UpdateLease(tmp_path, "new-agent") as lease:
        assert lease.operation_id == "new-agent"


# ── Fault-injection: corrupt audit chain ───────────────────────────────────


def test_audit_log_tamper_detected_on_append(tmp_path):
    """Tampering with a prior audit entry breaks the hash chain."""
    agent = _load_agent()
    agent.append_audit(tmp_path, {"operation_id": "x", "phase": "CHECKING"})
    agent.append_audit(tmp_path, {"operation_id": "x", "phase": "UPDATING"})
    lines = (tmp_path / "audit.jsonl").read_text().strip().split("\n")
    entries = [json.loads(line) for line in lines]
    # Tamper with first entry's phase
    entries[0]["phase"] = "TAMPERED"
    (tmp_path / "audit.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash chain"):
        agent.append_audit(tmp_path, {"operation_id": "x", "phase": "NORMAL"})


def test_audit_log_detects_truncated_entries(tmp_path):
    """Truncated or missing entries in audit log are detected."""
    agent = _load_agent()
    agent.append_audit(tmp_path, {"operation_id": "y", "phase": "CHECKING"})
    # Write a truncated line
    with open(tmp_path / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write('{"operation_id":"y","phase":"U')
    with pytest.raises((RuntimeError, json.JSONDecodeError)):
        agent.append_audit(tmp_path, {"operation_id": "y", "phase": "NORMAL"})


# ── Fault-injection: status.json corruption ────────────────────────────────


def test_recover_handles_empty_status_json(tmp_path, monkeypatch):
    """Empty status.json should be handled gracefully during recovery."""
    agent = _load_agent()
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    (tmp_path / "status.json").write_text("{}", encoding="utf-8")
    agent.recover_if_interrupted(Path("/nonexistent"), tmp_path)
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status.get("phase") in {"NORMAL", "RECOVERING", None}


def test_recover_handles_status_with_missing_fields(tmp_path, monkeypatch):
    """Status.json missing required fields should not crash recovery."""
    agent = _load_agent()
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    (tmp_path / "status.json").write_text(
        json.dumps({"state": "RUNNING"}), encoding="utf-8")
    agent.recover_if_interrupted(Path("/nonexistent"), tmp_path)
    # Should not raise; status may be updated or left as-is
    assert (tmp_path / "status.json").exists()


def test_recover_preserves_existing正常的_status(tmp_path, monkeypatch):
    """A NORMAL status without evidence should not be overwritten."""
    agent = _load_agent()
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    normal_status = {"state": "IDLE", "phase": "NORMAL", "progress": 100,
                     "message": "All good", "updated_at": "2026-01-01T00:00:00Z"}
    (tmp_path / "status.json").write_text(
        json.dumps(normal_status), encoding="utf-8")
    agent.recover_if_interrupted(Path("/nonexistent"), tmp_path)
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["phase"] == "NORMAL"


# ── Fault-injection: disk-full / fsync ─────────────────────────────────────


def test_atomic_json_raises_on_fsync_failure(tmp_path):
    """atomic_json propagates OSError when fsync fails (disk full)."""
    agent = _load_agent()
    target = tmp_path / "status.json"
    import os as _os
    original_fsync = _os.fsync

    def failing_fsync(fd):
        raise OSError(28, "No space left on device")
    _os.fsync = failing_fsync
    try:
        with pytest.raises(OSError, match="No space left on device"):
            agent.atomic_json(target, {"test": "data"})
    finally:
        _os.fsync = original_fsync


def test_append_audit_wraps_fsync_error(tmp_path):
    """append_audit raises when fsync fails during audit write."""
    agent = _load_agent()
    import os as _os
    original_fsync = _os.fsync
    fsync_called = [False]

    def failing_fsync(fd):
        fsync_called[0] = True
        raise OSError(28, "No space left on device")
    _os.fsync = failing_fsync
    try:
        with pytest.raises(OSError):
            agent.append_audit(tmp_path, {"operation_id": "disk-full", "phase": "CHECKING"})
        assert fsync_called[0], "fsync should have been called before OSError"
    finally:
        _os.fsync = original_fsync


# ── Fault-injection: drain / admission ─────────────────────────────────────


def test_wait_for_drain_polls_readiness(tmp_path, monkeypatch):
    """wait_for_drain polls /api/system/update/readiness until ready=True."""
    agent = _load_agent()
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("UPDATE_DRAIN_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("UPDATE_DRAIN_POLL_SECONDS", "0.01")
    call_count = [0]

    def fake_core_json(path):
        call_count[0] += 1
        if call_count[0] >= 3:
            return {"ready": True, "workers_drained": 2}
        return {"ready": False, "workers_drained": 0}
    monkeypatch.setattr(agent, "core_json", fake_core_json)
    state = {"state": "RUNNING", "phase": "CHECKING", "log": []}
    agent.wait_for_drain(tmp_path, state)
    assert call_count[0] >= 3
    assert state["readiness"]["ready"] is True


def test_wait_for_drain_timeout_raises(tmp_path, monkeypatch):
    """wait_for_drain raises RuntimeError when drain times out."""
    agent = _load_agent()
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("UPDATE_DRAIN_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("UPDATE_DRAIN_POLL_SECONDS", "0.01")
    monkeypatch.setattr(agent, "core_json", lambda path: {"ready": False})
    state = {"state": "RUNNING", "phase": "CHECKING", "log": []}
    with pytest.raises(RuntimeError, match="Timed out"):
        agent.wait_for_drain(tmp_path, state)


# ── Fault-injection: signed immutable compatibility ────────────────────────


def test_update_rejects_downgrade(tmp_path, monkeypatch):
    """update-agent rejects a manifest with version lower than current."""
    agent = _load_agent()
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    import core.update_protocol as _upd
    import core.version as _ver
    monkeypatch.setattr(_upd, "fetch_manifest", lambda channel: {
        "version": "0.0.1.0", "channel": "stable", "sha256": "a" * 64,
        "signature": "sig", "required": False})
    monkeypatch.setattr(_upd, "validate_manifest", lambda *a, **kw: None)
    monkeypatch.setattr(_upd, "validate_replay_state", lambda *a, **kw: None)
    monkeypatch.setattr(_ver, "application_version", lambda: "0.0.2.0")
    request = tmp_path / "requests" / "test.json"
    request.parent.mkdir()
    request.write_text(json.dumps({
        "request_id": "a" * 32, "action": "update",
        "target_version": "0.0.1.0"}), encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    result = agent.process_request(tmp_path, state_dir, request, skip_drain=True)
    assert result is False
    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert "downgrade" in status.get("message", "").lower()


def test_update_rejects_missing_root_metadata(tmp_path, monkeypatch):
    """update-agent rejects when REQUIRE_OFFLINE_ROOT=true but no root metadata."""
    agent = _load_agent()
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("REQUIRE_OFFLINE_ROOT", "true")
    monkeypatch.delenv("UPDATE_ROOT_METADATA", raising=False)
    monkeypatch.delenv("UPDATE_ROOT_KEYS", raising=False)
    import core.update_protocol as _upd
    monkeypatch.setattr(_upd, "fetch_manifest", lambda channel: {
        "version": "0.0.2.0", "channel": "stable", "sha256": "b" * 64,
        "signature": "sig", "required": False})
    monkeypatch.setattr(_upd, "validate_manifest", lambda *a, **kw: None)
    monkeypatch.setattr(_upd, "validate_replay_state", lambda *a, **kw: None)
    request = tmp_path / "requests" / "test.json"
    request.parent.mkdir()
    request.write_text(json.dumps({
        "request_id": "b" * 32, "action": "update"}), encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    result = agent.process_request(tmp_path, state_dir, request, skip_drain=True)
    assert result is False
    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert "root" in status.get("message", "").lower()


# ── Fault-injection: runtime / database recovery ───────────────────────────


def test_rollback_invokes_vertep_script(tmp_path, monkeypatch):
    """_attempt_rollback invokes vertep rollback script and sets ROLLED_BACK."""
    agent = _load_agent()
    commands_run = []
    monkeypatch.setattr(agent, "run", lambda cmd, root, **kw: (
        commands_run.append(cmd), "rollback output")[1])
    monkeypatch.setattr(agent, "atomic_json", lambda *a, **kw: None)
    import core.system_state as _ss
    monkeypatch.setattr(_ss, "set_system_state", lambda *a, **kw: None)
    state = {"state": "RUNNING", "phase": "UPDATING", "log": []}
    agent._attempt_rollback(tmp_path, tmp_path, state)
    assert any("rollback" in str(cmd) for cmd in commands_run)
    assert state["state"] == "ROLLED_BACK"


def test_process_request_triggers_rollback_on_failure(tmp_path, monkeypatch):
    """When apply-update fails, process_request triggers rollback."""
    agent = _load_agent()
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    import core.update_protocol as _upd
    import core.version as _ver
    monkeypatch.setattr(_upd, "fetch_manifest", lambda channel: {
        "version": "0.0.2.0", "channel": "stable", "sha256": "c" * 64,
        "signature": "sig", "required": False})
    monkeypatch.setattr(_upd, "validate_manifest", lambda *a, **kw: None)
    monkeypatch.setattr(_upd, "validate_replay_state", lambda *a, **kw: None)
    monkeypatch.setattr(_ver, "application_version", lambda: "0.0.1.0")

    call_count = [0]
    def failing_run(cmd, root, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("Health check failed")
        return "rollback ok"
    monkeypatch.setattr(agent, "run", failing_run)
    monkeypatch.setattr(agent, "transition", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "merge_runtime_progress", lambda *a, **kw: None)

    request = tmp_path / "requests" / "test.json"
    request.parent.mkdir()
    request.write_text(json.dumps({
        "request_id": "c" * 32, "action": "update"}), encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    result = agent.process_request(tmp_path, state_dir, request, skip_drain=True)
    assert result is False
    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status.get("state") in {"FAILED", "ROLLED_BACK"}
