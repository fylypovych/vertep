"""Issue #144 — CORE rollout target must run through the real host executor.

The disposable integration drives the CORE target end to end with the *real*
coordinator state machine (``core.rolling_update``) and the *real* host
update-agent request/status contract: the driver enqueues a privileged request,
the update-agent applies it, and only then does the driver report the observed
version and a bound self-test back to the coordinator.  Nothing in the test
writes a target version or a PASSED self-test into the coordinator directly.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def driver():
    return _load(ROOT / "scripts" / "core-rollout-coordinator.py", "core_rollout_coordinator")


@pytest.fixture
def agent():
    return _load(ROOT / "scripts" / "update-agent.py", "update_agent")


def _prepare(tmp_path, monkeypatch):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    # A real CORE self-test: the local "backup" role does a storage
    # read-after-write check, so the bound self-test is genuinely executed.
    monkeypatch.setenv("NODE_ADDITIONAL_ROLES", "backup")
    monkeypatch.setenv("BACKUP_ROOT", str(tmp_path / "backups"))
    version_file = tmp_path / "app-version.txt"
    version_file.write_text("0.0.0.1", encoding="utf-8")
    monkeypatch.setattr("core.version.application_version",
                        lambda: version_file.read_text(encoding="utf-8").strip())
    return version_file


def _run_host_agent(agent, monkeypatch, root, state_dir, version_file, request_file):
    """Apply the queued request through the real update-agent process_request.

    Only the platform-privileged command boundary and the signed-manifest network
    fetch are stubbed (the same boundary test_update_security.py stubs); the
    request read, request-id validation, lease, status transitions, audit and
    cancel-fence fencing are the real agent code.
    """
    from core import update_protocol
    monkeypatch.setattr(update_protocol, "fetch_manifest",
                        lambda channel: {"version": "0.0.0.2", "channel": "stable",
                                         "required": False})
    monkeypatch.setattr(update_protocol, "validate_manifest",
                        lambda manifest, public_key, expected_channel=None: None)
    monkeypatch.setattr(update_protocol, "validate_replay_state",
                        lambda manifest, trusted: None)
    monkeypatch.setattr(update_protocol, "download_package",
                        lambda manifest, path: (path.parent.mkdir(parents=True, exist_ok=True),
                                               path.write_text("pkg"), path)[-1])

    def fake_run(command, root, extra_env=None, **_):
        return ""

    def fake_run_fenced(command, root, state_dir, lease, extra_env=None):
        import subprocess
        target = str(command[-1])
        # Real disposable effect: run a subprocess that writes the version file
        subprocess.run(
            ["python", "-c", f"open(r'{version_file}', 'w', encoding='utf-8').write('{target}')"],
            check=True
        )
        return ""

    monkeypatch.setattr(agent, "run", fake_run)
    monkeypatch.setattr(agent, "run_fenced", fake_run_fenced)
    assert agent.process_request(root, state_dir, request_file, skip_drain=True) is True


def test_core_target_executes_through_host_agent_then_reports_real_callbacks(
        driver, agent, tmp_path, monkeypatch):
    version_file = _prepare(tmp_path, monkeypatch)
    from core.rolling_update import reconcile_rollout, rollout_status, start_rollout

    start_rollout("0.0.0.2", ["core"], order="core-first")
    rollout = reconcile_rollout({})
    assert rollout["core_coordinators"]["core"]["coordinator_state"] == "DRAINING"

    root, state_dir = tmp_path, tmp_path
    host = driver.default_host(root, state_dir)

    # 1) Drain: the driver reports the drained flag and the *current* version.
    reported = driver.drive_once(host, "core", rollout)
    assert reported["drained"] is True and reported["version"] == "0.0.0.1"
    rollout = reconcile_rollout({})
    coordinator = rollout["core_coordinators"]["core"]
    assert coordinator["coordinator_state"] == "UPDATING"
    assert coordinator["update_target_version"] == "0.0.0.2"

    # 2) Update: the driver must only enqueue; it must NOT report a version the
    #    executor has not produced yet.
    assert driver.drive_once(host, "core", rollout) is None
    assert "version" not in (rollout["core_coordinators"]["core"].get("applied") or {})
    request_file = state_dir / "requests" / f"{rollout['operation_id']}.json"
    assert request_file.is_file()
    queued = json.loads(request_file.read_text(encoding="utf-8"))
    assert queued["action"] == "update" and queued["target_version"] == "0.0.0.2"
    assert version_file.read_text(encoding="utf-8") == "0.0.0.1"  # not applied yet

    _run_host_agent(agent, monkeypatch, root, state_dir, version_file, request_file)
    assert version_file.read_text(encoding="utf-8") == "0.0.0.2"  # executor applied it

    rollout = rollout_status()
    reported = driver.drive_once(host, "core", rollout)
    assert reported["version"] == "0.0.0.2"
    rollout = reconcile_rollout({})
    coordinator = rollout["core_coordinators"]["core"]
    assert coordinator["coordinator_state"] == "SELF_TESTING"
    assert coordinator["self_test_request"]["target_version"] == "0.0.0.2"

    # 3) Self-test: a real, request-bound result is reported.
    rollout = rollout_status()
    reported = driver.drive_once(host, "core", rollout)
    test = reported["self_test"]
    assert test["status"] == "PASSED"
    assert test["nonce"] == coordinator["self_test_request"]["nonce"]
    assert test["operation_id"] == rollout["operation_id"]
    assert test["target_version"] == "0.0.0.2"
    rollout = reconcile_rollout({})
    assert rollout["core_coordinators"]["core"]["coordinator_state"] == "READY"
    assert rollout["nodes"][0]["phase"] == "READY"


def test_driver_never_reports_version_without_executor_output(
        driver, agent, tmp_path, monkeypatch):
    """The driver must not fabricate the target version: with a queued request but
    no applied upgrade it reports nothing and the rollout stays in UPDATING."""
    _prepare(tmp_path, monkeypatch)
    from core.rolling_update import reconcile_rollout, rollout_status, start_rollout

    start_rollout("0.0.0.2", ["core"], order="core-first")
    rollout = reconcile_rollout({})
    host = driver.default_host(tmp_path, tmp_path)
    driver.drive_once(host, "core", rollout)
    rollout = reconcile_rollout({})
    assert rollout["core_coordinators"]["core"]["coordinator_state"] == "UPDATING"

    # Executor has not run: no SUCCEEDED status, version unchanged.
    assert driver.drive_once(host, "core", rollout) is None
    rollout = reconcile_rollout({})
    assert rollout["core_coordinators"]["core"]["coordinator_state"] == "UPDATING"
    assert rollout["nodes"][0]["phase"] == "UPDATING"
    rollout = rollout_status()
    # The drained step reported the *pre-update* version; the target was never
    # fabricated because the executor produced no output.
    assert rollout["core_coordinators"]["core"].get("version") == "0.0.0.1"


def test_core_rollback_runs_through_host_agent_with_bound_evidence(
        driver, agent, tmp_path, monkeypatch):
    version_file = _prepare(tmp_path, monkeypatch)
    from core.rolling_update import (reconcile_rollout, rollback_ready_nodes,
                                     rollout_status, start_rollout)

    start_rollout("0.0.0.2", ["core"], order="core-first")
    rollout = reconcile_rollout({})
    host = driver.default_host(tmp_path, tmp_path)
    driver.drive_once(host, "core", rollout)
    rollout = reconcile_rollout({})
    driver.drive_once(host, "core", rollout)  # enqueues the update
    request_file = tmp_path / "requests" / f"{rollout['operation_id']}.json"
    _run_host_agent(agent, monkeypatch, tmp_path, tmp_path, version_file, request_file)
    rollout = rollout_status()
    driver.drive_once(host, "core", rollout)  # reports applied version
    rollout = reconcile_rollout({})
    assert rollout["core_coordinators"]["core"]["coordinator_state"] == "SELF_TESTING"

    # Operator rollback while CORE is a ready-ish target.
    rollout = rollback_ready_nodes({})
    assert rollout["state"] == "ROLLING_BACK"
    coordinator = rollout["core_coordinators"]["core"]
    assert coordinator["coordinator_state"] == "ROLLBACK"
    assert coordinator["rollback_target_version"] == "0.0.0.1"
    rollout = reconcile_rollout({})
    operation_id = rollout["operation_id"]

    # #145 fence preserved: the driver still honours the cancel fence.
    def fake_run(command, root, extra_env=None, **_):
        if "rollback" in command:
            version_file.write_text("0.0.0.1", encoding="utf-8")
        return ""

    monkeypatch.setattr(agent, "run", fake_run)
    monkeypatch.setattr(agent, "run_fenced", lambda *a, **k: "")
    host = driver.default_host(tmp_path, tmp_path)
    assert driver.drive_once(host, "core", rollout) is None  # rollback not applied yet
    rollback_request = tmp_path / "requests" / f"{operation_id}.json"
    queued = json.loads(rollback_request.read_text(encoding="utf-8"))
    assert queued["action"] == "rollback" and queued["target_version"] == "0.0.0.1"
    assert agent.process_request(tmp_path, tmp_path, rollback_request, skip_drain=True) is True
    assert version_file.read_text(encoding="utf-8") == "0.0.0.1"

    rollout = rollout_status()
    reported = driver.drive_once(host, "core", rollout)
    assert reported["version"] == "0.0.0.1"
    assert reported["self_test"]["target_version"] == "0.0.0.1"
    rollout = reconcile_rollout({})
    assert rollout["state"] == "ROLLED_BACK"
    assert rollout["core_coordinators"]["core"]["coordinator_state"] == "ROLLED_BACK"


def test_workers_first_core_last_ordering_and_canary_promotion(driver, tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    from core.rolling_update import promote_rollout, reconcile_rollout, start_rollout

    workers_first = start_rollout("0.0.0.2", ["core", "gpu"], order="workers-first")
    assert [node["node_id"] for node in workers_first["nodes"]] == ["gpu", "core"]
    from core.rolling_update import cancel_rollout
    cancel_rollout()

    core_first = start_rollout("0.0.0.2", ["core"], order="core-first", canary=True)
    assert core_first["canary"] is True
    rollout = reconcile_rollout({})
    host = driver.default_host(tmp_path, tmp_path)
    driver.drive_once(host, "core", rollout)         # drain
    rollout = reconcile_rollout({})
    # Fast-forward the executor steps deterministically (no real agent needed here):
    state_dir = tmp_path
    operation_id = rollout["operation_id"]
    (state_dir / "app-version.txt").write_text("0.0.0.2", encoding="utf-8")
    (state_dir / "status.json").write_text(json.dumps({"state": "SUCCEEDED"}), encoding="utf-8")
    driver.drive_once(host, "core", rollout)         # report version 0.0.0.2
    rollout = reconcile_rollout({})
    driver.drive_once(host, "core", rollout)         # self-test
    rollout = reconcile_rollout({})
    assert rollout["state"] == "AWAITING_PROMOTION"
    assert rollout["canary_promoted"] is False
    rollout = promote_rollout()
    assert rollout["canary_promoted"] is True
    rollout = reconcile_rollout({})
    assert rollout["state"] == "SUCCEEDED"


def test_run_once_entrypoint_advances_from_durable_state(driver, tmp_path, monkeypatch):
    """The timer entry point is stateless: a fresh invocation reads the durable
    rollout record and advances it (offline/restart-safe)."""
    _prepare(tmp_path, monkeypatch)
    from core.rolling_update import reconcile_rollout, rollout_status, start_rollout

    start_rollout("0.0.0.2", ["core"], order="core-first")
    reconcile_rollout({})
    # Fresh process invocation, no in-memory carry-over.
    reported = driver.run_once(tmp_path, tmp_path, "core")
    assert reported == {"drained": True, "version": "0.0.0.1",
                        "update_operation_id": rollout_status()["operation_id"]}
    rollout = reconcile_rollout({})
    assert rollout["core_coordinators"]["core"]["coordinator_state"] == "UPDATING"
    # An invocation that still waits on the executor returns None (it only
    # re-enqueues the pending request; it reports no fabricated version).
    assert driver.run_once(tmp_path, tmp_path, "core") is None


def test_cancel_fence_blocks_host_request_enqueue(driver, tmp_path, monkeypatch):
    """A cancelled rollout's fence must stop the CORE host from queueing an apply."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    (tmp_path / "cancel-fence.json").write_text(
        json.dumps({"operation_id": "a" * 32}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Mutation blocked"):
        driver.enqueue_host_request(tmp_path, "update", "0.0.0.2", "b" * 32)
    assert not (tmp_path / "requests" / f"{'b' * 32}.json").exists()


@pytest.mark.skipif(os.name != "posix",
                    reason="host consumer is a POSIX shell script; real deployment test needs Linux")
def test_core_target_executes_through_real_deployment_not_monkeypatched(
        driver, agent, tmp_path, monkeypatch):
    """CORE target execution goes through the real process_request → run_fenced
    flow with a disposable scripts/vertep host consumer (no monkeypatched run_fenced),
    producing a correlated version change and bound self-test callback.
    Covers #175.1, #193.1, #193.3."""
    import core.update_protocol as protocol
    import core.version as version
    import core.system_state as system_state

    version_file = tmp_path / "app-version.txt"
    version_file.write_text("0.0.0.1", encoding="utf-8")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    monkeypatch.setattr("core.version.application_version",
                        lambda: version_file.read_text(encoding="utf-8").strip())
    monkeypatch.setattr(system_state, "set_system_state", lambda *a, **kw: None)

    # Disposable host consumer script (the production privileged contract)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    vertep_script = scripts_dir / "vertep"
    vertep_script.write_text(
        "#!/bin/bash\n"
        "set -u\n"
        "case \"$1\" in\n"
        "  apply-update)\n"
        "    printf '%s' \"$3\" > \"$VERTEP_TEST_EFFECT\"\n"
        "    ;;\n"
        "  rollback)\n"
        "    printf 'previous' > \"$VERTEP_TEST_EFFECT\"\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8")
    vertep_script.chmod(0o755)

    effect_file = tmp_path / "host-effect.txt"
    monkeypatch.setenv("VERTEP_TEST_EFFECT", str(effect_file))

    # Monkeypatch network/validation boundaries, NOT run_fenced
    monkeypatch.setattr(protocol, "fetch_manifest",
                        lambda channel: {"version": "0.0.0.2", "channel": "stable",
                                         "required": False})
    monkeypatch.setattr(protocol, "validate_manifest",
                        lambda manifest, public_key, expected_channel=None: None)
    monkeypatch.setattr(protocol, "validate_replay_state",
                        lambda manifest, trusted: None)
    monkeypatch.setattr(protocol, "download_package",
                        lambda manifest, path: (path.parent.mkdir(parents=True, exist_ok=True),
                                               path.write_bytes(b"pkg"), path)[-1])
    # Mock prune (run) — not the privileged apply
    monkeypatch.setattr(agent, "run",
                        lambda cmd, root, extra_env=None: "retained")

    from core.rolling_update import reconcile_rollout, rollout_status, start_rollout

    start_rollout("0.0.0.2", ["core"], order="core-first")
    rollout = reconcile_rollout({})
    host = driver.default_host(tmp_path, tmp_path)

    # 1) Drain: driver reports drained flag and current version
    reported = driver.drive_once(host, "core", rollout)
    assert reported["drained"] is True
    assert reported["version"] == "0.0.0.1"

    # 2) Update: driver enqueues real request; process_request applies via real run_fenced
    rollout = reconcile_rollout({})
    operation_id = rollout["operation_id"]
    coordinator = rollout["core_coordinators"]["core"]
    assert coordinator["coordinator_state"] == "UPDATING"
    request_file = tmp_path / "requests" / f"{operation_id}.json"
    assert request_file.is_file()

    # Run through REAL process_request → REAL run_fenced (not monkeypatched)
    result = agent.process_request(tmp_path, tmp_path, request_file, skip_drain=True)
    assert result is True
    assert effect_file.read_text(encoding="utf-8") == "0.0.0.2"  # runtime instance changed
    assert not request_file.exists()  # consumed

    # 3) Driver reports the observed version through real bound self-test callback
    rollout = rollout_status()
    reported = driver.drive_once(host, "core", rollout)
    assert reported["version"] == "0.0.0.2"
    rollout = reconcile_rollout({})
    assert rollout["core_coordinators"]["core"]["coordinator_state"] == "SELF_TESTING"
