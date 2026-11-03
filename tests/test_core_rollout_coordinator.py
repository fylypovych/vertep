"""Issue #144/#193 — CORE rollout target must run through the real host executor.

The disposable integration drives the CORE target end to end with the *real*
coordinator state machine (``core.rolling_update``) and the *real* host
update-agent request/status contract: the driver enqueues a privileged request,
the update-agent applies it through the real ``run_fenced`` fence, the host
consumer replaces the active runtime instance, and only then does the driver
report the observed version and a bound self-test back to the coordinator.
Nothing in the test writes a target version or a PASSED self-test into the
coordinator directly.
"""

import importlib.util
import json
import os
import shutil
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _distributed_fence_dsn() -> str | None:
    """Return the disposable PostgreSQL boundary only when CI requires it."""
    dsn = os.getenv("VERTEP_UPDATE_LEASE_DATABASE_URL", "")
    required = os.getenv("VERTEP_REQUIRE_DISTRIBUTED_UPDATE_FENCE") == "1"
    if required and not dsn:
        pytest.fail("VERTEP_UPDATE_LEASE_DATABASE_URL is required for PostgreSQL fencing")
    return dsn or None


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


def _prepare(root: Path, state_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("VERTEP_ROOT", str(root))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    # A real CORE self-test: the local "backup" role does a storage
    # read-after-write check, so the bound self-test is genuinely executed.
    monkeypatch.setenv("NODE_ADDITIONAL_ROLES", "backup")
    monkeypatch.setenv("BACKUP_ROOT", str(state_dir / "backups"))


def _release_package(tmp_path: Path, version: str) -> Path:
    """Build a real release archive for the disposable deployment."""
    payload = tmp_path / f"payload-{version}"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "VERSION").write_text(version, encoding="utf-8")
    (payload / "application.txt").write_text(f"runtime {version}", encoding="utf-8")
    archive = tmp_path / f"vertep-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        package.add(payload, arcname=payload.name)
    return archive


def _active_version(root: Path) -> str:
    return (root / "current" / "VERSION").read_text(encoding="utf-8").strip()


def _health(root: Path, record: dict | None = None) -> dict | None:
    """Probe the running runtime instance (the recorded one by default)."""
    import urllib.request

    if record is None:
        record = json.loads((root / "runtime" / "instance.json").read_text(encoding="utf-8"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{record['port']}/api/health", timeout=1) as response:
            return json.loads(response.read().decode("utf-8"))
    except OSError:
        return None


def _journal(state_dir: Path) -> list[dict]:
    path = state_dir / "audit.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_core_target_executes_through_real_deployment_not_monkeypatched(
        driver, agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server):
    """CORE target execution goes through the real process_request → run_fenced
    flow with a disposable installation whose host consumer replaces the active
    runtime instance, producing a correlated version change and a bound
    self-test callback.
    Covers #175.1, #193.1, #193.3."""
    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _prepare(root, state_dir, monkeypatch)
    archive = _release_package(tmp_path, "0.0.0.2")
    signed_update_server.publish("0.0.0.2", archive)

    from core.rolling_update import reconcile_rollout, rollout_status, start_rollout

    start_rollout("0.0.0.2", ["core"], order="core-first")
    rollout = reconcile_rollout({})
    host = driver.default_host(root, state_dir)

    # 1) Drain: the driver reports the drained flag and the *current* version.
    reported = driver.drive_once(host, "core", rollout)
    assert reported["drained"] is True
    assert reported["version"] == "0.0.0.1"

    # 2) Update: the driver enqueues a real host request and reports nothing
    #    before the executor produced an applied instance.
    rollout = reconcile_rollout({})
    operation_id = rollout["operation_id"]
    assert driver.drive_once(host, "core", rollout) is None
    request_file = state_dir / "requests" / f"{operation_id}.json"
    assert request_file.is_file()
    queued = json.loads(request_file.read_text(encoding="utf-8"))
    assert queued["action"] == "update" and queued["target_version"] == "0.0.0.2"
    assert _active_version(root) == "0.0.0.1"
    before = _health(root)
    assert before["version"] == "0.0.0.1"

    assert agent.process_request(root, state_dir, request_file, skip_drain=True) is True

    # The running instance was stopped and a new one serves the target version,
    # acknowledging the operation that replaced it.
    after = _health(root)
    assert after["instance"] != before["instance"] and after["pid"] != before["pid"]
    assert after["version"] == "0.0.0.2"
    assert after["operation_id"] == operation_id
    assert _health(root, before) is None, "the previous runtime instance is still serving"

    # The runtime instance itself was replaced; the previous release is kept.
    assert _active_version(root) == "0.0.0.2"
    assert (root / "VERSION").read_text(encoding="utf-8").strip() == "0.0.0.2"
    assert (root / "releases" / "0.0.0.1" / "VERSION").is_file()
    assert (root / "releases" / "0.0.0.2" / "VERSION").is_file()
    assert not request_file.exists()

    # 3) The driver reports the version observed on the active instance and a
    #    real, request-bound self-test.
    rollout = rollout_status()
    reported = driver.drive_once(host, "core", rollout)
    assert reported["version"] == "0.0.0.2"
    rollout = reconcile_rollout({})
    coordinator = rollout["core_coordinators"]["core"]
    assert coordinator["coordinator_state"] == "SELF_TESTING"

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

    journal = _journal(state_dir)
    assert journal, "the executor must leave a correlated journal"
    assert all(event.get("operation_id") == operation_id for event in journal)
    assert all(event.get("event_hash") for event in journal)


def test_worker_request_reaches_host_consumer_and_reports_correlated_result(
        agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server):
    """A Worker update request reaches the standard host consumer, replaces the
    runtime instance and is acknowledged with a correlated result (#175.1)."""
    from worker.service import request_local_update

    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    monkeypatch.setenv("UPDATE_REQUEST_DIR", str(requests))
    archive = _release_package(tmp_path, "0.0.0.2")
    signed_update_server.publish("0.0.0.2", archive)

    request_local_update("0.0.0.2")
    request_file = next(requests.glob("*.json"))
    request_id = request_file.stem
    queued = json.loads(request_file.read_text(encoding="utf-8"))
    assert queued["action"] == "update" and queued["target_version"] == "0.0.0.2"

    assert agent.process_request(root, state_dir, request_file, skip_drain=True) is True
    assert _active_version(root) == "0.0.0.2"
    assert not request_file.exists()

    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "SUCCEEDED" and status["progress"] == 100
    assert status["request_id"] == request_id
    journal = _journal(state_dir)
    assert journal and all(event.get("operation_id") == request_id for event in journal)


def test_failed_apply_rolls_back_runtime_instance_and_can_be_retried(
        agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server):
    """A fault during the host apply restores the previous runtime instance,
    journals the correlated failure and leaves the node ready for a retry
    (#175.2)."""
    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    archive = _release_package(tmp_path, "0.0.0.2")
    signed_update_server.publish("0.0.0.2", archive)
    (root / "fail-migration").write_text("1", encoding="utf-8")

    failed = requests / ("f" * 32 + ".json")
    failed.write_text(json.dumps({"request_id": "f" * 32, "action": "update",
                                  "target_version": "0.0.0.2"}), encoding="utf-8")
    assert agent.process_request(root, state_dir, failed, skip_drain=True) is False

    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "ROLLED_BACK"
    assert _active_version(root) == "0.0.0.1"
    rolled_back = _health(root)
    assert rolled_back["version"] == "0.0.0.1" and rolled_back["operation_id"] == "f" * 32
    assert not failed.exists()
    journal = _journal(state_dir)
    assert journal and all(event.get("operation_id") == "f" * 32 for event in journal)

    (root / "fail-migration").unlink()
    retry_archive = _release_package(tmp_path, "0.0.0.3")
    signed_update_server.publish("0.0.0.3", retry_archive)
    retry = requests / ("a" * 32 + ".json")
    retry.write_text(json.dumps({"request_id": "a" * 32, "action": "update",
                                 "target_version": "0.0.0.3"}), encoding="utf-8")
    assert agent.process_request(root, state_dir, retry, skip_drain=True) is True
    assert _active_version(root) == "0.0.0.3"
    retried = _health(root)
    assert retried["version"] == "0.0.0.3" and retried["instance"] != rolled_back["instance"]
    assert _health(root, rolled_back) is None
    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "SUCCEEDED" and status["request_id"] == "a" * 32


def test_restart_request_replaces_running_runtime_instance(
        agent, disposable_deployment, tmp_path, monkeypatch):
    """A restart request is executed by the host consumer on the running
    runtime: the old instance stops, a new one serves health for the same
    release and acknowledges the restart operation (#174.4, #193.1)."""
    from worker.service import request_local_update

    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    monkeypatch.setenv("UPDATE_REQUEST_DIR", str(requests))
    before = _health(root)
    operation_id = "c" * 32

    request_local_update("current", action="restart", request_id=operation_id)
    assert agent.process_request(
        root, state_dir, requests / f"{operation_id}.json", skip_drain=True) is True

    after = _health(root)
    assert after["instance"] != before["instance"] and after["pid"] != before["pid"]
    assert after["version"] == "0.0.0.1" and after["operation_id"] == operation_id
    assert _health(root, before) is None
    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["request_id"] == operation_id and status["state"] == "SUCCEEDED"


def test_cancel_during_active_restart_stops_consumer_without_delayed_restart(
        agent, disposable_deployment, tmp_path, monkeypatch):
    """A cancel fence written while the host consumer is already restarting
    kills the consumer; the running instance is untouched and no delayed
    restart lands after the cancelled ack (#175.2, #193.4, #193.6)."""
    from worker.service import request_local_update

    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    monkeypatch.setenv("UPDATE_REQUEST_DIR", str(requests))
    monkeypatch.setenv("UPDATE_FENCE_CHECK_SECONDS", "0.05")
    monkeypatch.setenv("UPDATE_FENCE_KILL_TIMEOUT", "5")
    (root / "restart-delay").write_text("2", encoding="utf-8")
    before = _health(root)
    operation_id = "e" * 32
    request_local_update("current", action="restart", request_id=operation_id)

    def cancel() -> None:
        time.sleep(0.5)
        (state_dir / "cancel-fence.json").write_text(
            json.dumps({"operation_id": operation_id}), encoding="utf-8")

    canceller = threading.Thread(target=cancel)
    canceller.start()
    assert agent.process_request(
        root, state_dir, requests / f"{operation_id}.json", skip_drain=True) is False
    canceller.join()

    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED" and "cancelled" in status["message"]
    time.sleep(3)
    assert _health(root) == before, "a cancelled restart still replaced the runtime"


def test_interrupted_apply_recovers_previous_runtime_instance_after_restart(
        agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server):
    """A host apply interrupted by a restart is recovered by rolling the active
    runtime instance back to the previous release (#175.2, #193.2)."""
    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    archive = _release_package(tmp_path, "0.0.0.2")
    signed_update_server.publish("0.0.0.2", archive)

    request_file = requests / ("b" * 32 + ".json")
    request_file.write_text(json.dumps({"request_id": "b" * 32, "action": "update",
                                        "target_version": "0.0.0.2"}), encoding="utf-8")
    assert agent.process_request(root, state_dir, request_file, skip_drain=True) is True
    assert _active_version(root) == "0.0.0.2"

    # The host agent died in the middle of the apply; the durable status is the
    # only evidence the new agent has after the restart.
    status_path = state_dir / "status.json"
    interrupted = json.loads(status_path.read_text(encoding="utf-8"))
    interrupted.update({"state": "RUNNING", "phase": "UPDATING"})
    status_path.write_text(json.dumps(interrupted), encoding="utf-8")

    agent.recover_if_interrupted(root, state_dir)

    recovered = json.loads(status_path.read_text(encoding="utf-8"))
    assert recovered["state"] == "ROLLED_BACK"
    assert _active_version(root) == "0.0.0.1"
    journal = _journal(state_dir)
    assert any(event.get("message") == "Previous release restored" for event in journal)


def test_mid_apply_lease_takeover_stops_the_real_mutation(tmp_path, monkeypatch):
    """A lease taken over by another agent during an apply stops the privileged
    mutation itself; its delayed side effect must never land (#193.4, #193.6)."""
    agent = _load(ROOT / "scripts" / "update-agent.py", "update_agent")
    from core.update_lease import UpdateLease

    monkeypatch.setenv("UPDATE_FENCE_CHECK_SECONDS", "0.02")
    monkeypatch.setenv("UPDATE_FENCE_KILL_TIMEOUT", "5")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    marker = tmp_path / "side-effect.txt"
    script = (
        "import json, sys, time\n"
        "lease = sys.argv[1]\n"
        "record = json.loads(open(lease, encoding='utf-8').read())\n"
        "record['operation_id'] = 'b' * 32\n"
        "open(lease, 'w', encoding='utf-8').write(json.dumps(record))\n"
        "time.sleep(3.0)\n"
        "open(sys.argv[2], 'w', encoding='utf-8').write('applied')\n"
    )
    with UpdateLease(state_dir, "a" * 32) as lease:
        with pytest.raises(RuntimeError, match="taken over"):
            agent.run_fenced([sys.executable, "-c", script, str(lease.path), str(marker)],
                             tmp_path, state_dir, lease)
    assert not marker.exists(), "the fenced apply still produced its delayed side effect"


def test_mid_apply_fence_store_loss_stops_the_real_mutation(tmp_path, monkeypatch):
    """A lost database fence during an apply stops the privileged mutation
    instead of letting it run unfenced (#193.4, #193.6)."""
    agent = _load(ROOT / "scripts" / "update-agent.py", "update_agent")
    from core.update_lease import UpdateLease

    monkeypatch.setenv("UPDATE_FENCE_CHECK_SECONDS", "0.02")
    monkeypatch.setenv("UPDATE_FENCE_KILL_TIMEOUT", "5")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    marker = tmp_path / "side-effect.txt"
    script = (
        "import sys, time\n"
        "time.sleep(3.0)\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write('applied')\n"
    )

    class LostDatabase:
        def execute(self, query, *args):
            if "FROM update_fences" in query:
                raise RuntimeError("connection lost")
            return None

        def close(self):
            return None

    with UpdateLease(state_dir, "a" * 32) as lease:
        lease._database = LostDatabase()
        with pytest.raises(RuntimeError, match="fence is unavailable"):
            agent.run_fenced([sys.executable, "-c", script, str(marker)],
                             tmp_path, state_dir, lease)
    assert not marker.exists(), "the apply ran on while its fence store was unavailable"


def test_postgres_epoch_takeover_stops_active_host_apply(tmp_path, monkeypatch):
    """A real PostgreSQL epoch takeover kills an already-started host apply.

    This is deliberately separate from the file-lease regressions above: a
    second database connection advances the exact epoch read by ``run_fenced``.
    The command is a real child process and its delayed write must not survive
    the fenced process group.
    """
    dsn = _distributed_fence_dsn()
    if not dsn:
        pytest.skip("disposable PostgreSQL fencing environment is not configured")
    import psycopg

    agent = _load(ROOT / "scripts" / "update-agent.py", "update_agent_postgres_fence")
    from core.update_lease import UpdateLease

    monkeypatch.setenv("UPDATE_DATABASE_URL", dsn)
    monkeypatch.setenv("UPDATE_REQUIRE_DISTRIBUTED_FENCE", "true")
    monkeypatch.setenv("UPDATE_FENCE_CHECK_SECONDS", "0.02")
    monkeypatch.setenv("UPDATE_FENCE_KILL_TIMEOUT", "5")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    marker = tmp_path / "delayed-side-effect.txt"
    operation_id, replacement = "a" * 32, "b" * 32
    script = (
        "import sys, time\n"
        "time.sleep(1.0)\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write('applied')\n"
    )

    def take_over_epoch():
        time.sleep(0.15)
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                "UPDATE update_fences SET epoch=epoch+1, operation_id=%s WHERE name='global'",
                (replacement,),
            )

    with UpdateLease(state_dir, operation_id) as lease:
        takeover = threading.Thread(target=take_over_epoch)
        takeover.start()
        with pytest.raises(RuntimeError, match="distributed update fence was lost"):
            agent.run_fenced([sys.executable, "-c", script, str(marker)],
                             tmp_path, state_dir, lease)
        takeover.join(timeout=5)
        assert not takeover.is_alive(), "PostgreSQL takeover thread did not finish"
    assert not marker.exists(), "an apply fenced by PostgreSQL still mutated the runtime"


def test_driver_never_reports_version_without_executor_output(
        driver, disposable_deployment, tmp_path, monkeypatch):
    """The driver must not fabricate the target version: with a queued request but
    no applied upgrade it reports nothing and the rollout stays in UPDATING."""
    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _prepare(root, state_dir, monkeypatch)

    from core.rolling_update import reconcile_rollout, rollout_status, start_rollout

    start_rollout("0.0.0.2", ["core"], order="core-first")
    rollout = reconcile_rollout({})
    host = driver.default_host(root, state_dir)
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
        driver, agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server):
    """Rollback runs through the standard host consumer and restores the
    previous runtime instance with a bound self-test (#193.2)."""
    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _prepare(root, state_dir, monkeypatch)
    archive = _release_package(tmp_path, "0.0.0.2")
    signed_update_server.publish("0.0.0.2", archive)

    from core.rolling_update import (reconcile_rollout, rollback_ready_nodes,
                                     rollout_status, start_rollout)

    start_rollout("0.0.0.2", ["core"], order="core-first")
    rollout = reconcile_rollout({})
    host = driver.default_host(root, state_dir)
    driver.drive_once(host, "core", rollout)
    rollout = reconcile_rollout({})
    driver.drive_once(host, "core", rollout)
    request_file = state_dir / "requests" / f"{rollout['operation_id']}.json"
    assert agent.process_request(root, state_dir, request_file, skip_drain=True) is True
    assert _active_version(root) == "0.0.0.2"

    rollout = rollout_status()
    driver.drive_once(host, "core", rollout)
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

    # The driver still honours the cancel fence and reports nothing before the
    # executor produced the restored instance.
    assert driver.drive_once(host, "core", rollout) is None
    rollback_request = state_dir / "requests" / f"{operation_id}.json"
    queued = json.loads(rollback_request.read_text(encoding="utf-8"))
    assert queued["action"] == "rollback" and queued["target_version"] == "0.0.0.1"
    updated = _health(root)
    assert updated["version"] == "0.0.0.2"
    assert agent.process_request(root, state_dir, rollback_request, skip_drain=True) is True
    assert _active_version(root) == "0.0.0.1"
    restored = _health(root)
    assert restored["version"] == "0.0.0.1" and restored["operation_id"] == operation_id
    assert _health(root, updated) is None, "the rolled-back instance is still serving"

    rollout = rollout_status()
    reported = driver.drive_once(host, "core", rollout)
    assert reported["version"] == "0.0.0.1"
    assert reported["self_test"]["target_version"] == "0.0.0.1"
    rollout = reconcile_rollout({})
    assert rollout["state"] == "ROLLED_BACK"
    assert rollout["core_coordinators"]["core"]["coordinator_state"] == "ROLLED_BACK"


def test_workers_first_core_last_ordering_and_canary_promotion(
        driver, agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server):
    """Workers run before CORE, the canary waits for an explicit promotion and
    the whole flow is proven by the real executor (#193.2)."""
    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _prepare(root, state_dir, monkeypatch)
    archive = _release_package(tmp_path, "0.0.0.2")
    signed_update_server.publish("0.0.0.2", archive)

    from core.rolling_update import (cancel_rollout, promote_rollout, reconcile_rollout,
                                     rollout_status, start_rollout)

    workers_first = start_rollout("0.0.0.2", ["core", "gpu"], order="workers-first")
    assert [node["node_id"] for node in workers_first["nodes"]] == ["gpu", "core"]
    cancel_rollout()

    core_first = start_rollout("0.0.0.2", ["core"], order="core-first", canary=True)
    assert core_first["canary"] is True
    rollout = reconcile_rollout({})
    host = driver.default_host(root, state_dir)
    driver.drive_once(host, "core", rollout)          # drain
    rollout = reconcile_rollout({})
    driver.drive_once(host, "core", rollout)          # enqueue the update
    rollout = rollout_status()
    request_file = state_dir / "requests" / f"{rollout['operation_id']}.json"
    assert request_file.is_file()
    assert agent.process_request(root, state_dir, request_file, skip_drain=True) is True

    rollout = rollout_status()
    reported = driver.drive_once(host, "core", rollout)
    assert reported["version"] == "0.0.0.2"
    rollout = reconcile_rollout({})
    driver.drive_once(host, "core", rollout)          # bound self-test
    rollout = reconcile_rollout({})
    assert rollout["state"] == "AWAITING_PROMOTION"
    assert rollout["canary_promoted"] is False
    rollout = promote_rollout()
    assert rollout["canary_promoted"] is True
    rollout = reconcile_rollout({})
    assert rollout["state"] == "SUCCEEDED"


def test_run_once_entrypoint_advances_from_durable_state(
        driver, disposable_deployment, tmp_path, monkeypatch):
    """The timer entry point is stateless: a fresh invocation reads the durable
    rollout record and advances it (offline/restart-safe)."""
    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _prepare(root, state_dir, monkeypatch)

    from core.rolling_update import reconcile_rollout, rollout_status, start_rollout

    start_rollout("0.0.0.2", ["core"], order="core-first")
    reconcile_rollout({})
    # Fresh process invocation, no in-memory carry-over.
    reported = driver.run_once(root, state_dir, "core")
    assert reported == {"drained": True, "version": "0.0.0.1",
                        "update_operation_id": rollout_status()["operation_id"]}
    rollout = reconcile_rollout({})
    assert rollout["core_coordinators"]["core"]["coordinator_state"] == "UPDATING"
    # An invocation that still waits on the executor returns None (it only
    # re-enqueues the pending request; it reports no fabricated version).
    assert driver.run_once(root, state_dir, "core") is None


def test_cancel_fence_blocks_host_request_enqueue(driver, tmp_path, monkeypatch):
    """A cancelled rollout's fence must stop the CORE host from queueing an apply."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    (tmp_path / "cancel-fence.json").write_text(
        json.dumps({"operation_id": "a" * 32}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Mutation blocked"):
        driver.enqueue_host_request(tmp_path, "update", "0.0.0.2", "b" * 32)
    assert not (tmp_path / "requests" / f"{'b' * 32}.json").exists()


def test_tampered_signed_manifest_is_refused_before_any_host_mutation(
        agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server):
    """The real signature check of the update agent refuses a manifest whose
    package digest was altered after signing; nothing is downloaded, applied or
    restarted, and the refusal is correlated with the request (#193.4)."""
    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    signed_update_server.publish("0.0.0.2", _release_package(tmp_path, "0.0.0.2"), tamper=True)
    before = _health(root)
    request = requests / ("9" * 32 + ".json")
    request.write_text(json.dumps({"request_id": "9" * 32, "action": "update",
                                   "target_version": "0.0.0.2"}), encoding="utf-8")

    assert agent.process_request(root, state_dir, request, skip_drain=True) is False

    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED" and status["request_id"] == "9" * 32
    assert "signature" in status["message"].lower()
    assert _active_version(root) == "0.0.0.1"
    assert not (root / "releases" / "0.0.0.2").exists()
    assert not (state_dir / "packages").exists() or not any((state_dir / "packages").iterdir())
    assert _health(root) == before, "a refused manifest still restarted the runtime"


@pytest.mark.parametrize("member", ["symlink", "traversal"])
def test_a_signed_unsafe_archive_is_refused_by_the_executor_without_activation(
        agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server, member):
    """#394 B2: a correctly signed package whose archive escapes the staging root (a
    symlink member or a ../ path) is refused by the executor's safe extraction; the
    new release is never activated and the previous runtime keeps serving."""
    import io

    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    archive = tmp_path / "vertep-0.0.0.2.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        version = b"0.0.0.2"
        info = tarfile.TarInfo("payload-0.0.0.2/VERSION")
        info.size = len(version)
        package.addfile(info, io.BytesIO(version))
        if member == "symlink":
            link = tarfile.TarInfo("payload-0.0.0.2/escape")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc"
            package.addfile(link)
        else:
            outside = tarfile.TarInfo("../outside.txt")
            outside.size = 1
            package.addfile(outside, io.BytesIO(b"x"))
    signed_update_server.publish("0.0.0.2", archive)
    request = requests / ("6" * 32 + ".json")
    request.write_text(json.dumps({"request_id": "6" * 32, "action": "update",
                                   "target_version": "0.0.0.2"}), encoding="utf-8")

    assert agent.process_request(root, state_dir, request, skip_drain=True) is False

    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["request_id"] == "6" * 32 and status["state"] in {"FAILED", "ROLLED_BACK"}
    assert _active_version(root) == "0.0.0.1"
    assert not (root / "releases" / "0.0.0.2").exists()
    assert not (tmp_path / "outside.txt").exists() and not (root.parent / "outside.txt").exists()
    assert _health(root)["version"] == "0.0.0.1"


@pytest.mark.parametrize(("dimension", "revoked", "trusted_version", "message"), [
    ("revoke", True, 0, "revoked"),
    ("rotation", False, 9, "rollback"),
], ids=["revoke", "rotation"])
def test_release_key_trust_is_enforced_by_the_executor_before_any_host_mutation(
        agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server,
        dimension, revoked, trusted_version, message):
    """#394 B2: with offline-root metadata the update executor refuses a release signed
    by a revoked key, and root metadata rolled back below the trusted version (a key
    rotation replayed backwards); release, packages and runtime stay unchanged."""
    import base64
    import hashlib
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from core.update_trust import canonical_metadata

    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    keyring, root_keys = tmp_path / "keyring", tmp_path / "root-keys"
    keyring.mkdir()
    root_keys.mkdir()
    shutil.copyfile(signed_update_server.public_key, keyring / "release-1.pem")
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (root_keys / "root-1.pem").write_bytes(root_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    metadata = {"version": 4, "expires_at": "2099-01-01T00:00:00Z", "threshold": 1,
                "release_keys": {"release-1": {
                    "sha256": hashlib.sha256((keyring / "release-1.pem").read_bytes()).hexdigest(),
                    "channels": ["stable"], "revoked": revoked}}}
    metadata["signatures"] = [{"key_id": "root-1", "signature": base64.b64encode(root_key.sign(
        canonical_metadata(metadata), padding.PKCS1v15(), hashes.SHA256())).decode()}]
    (tmp_path / "root.json").write_text(json.dumps(metadata), encoding="utf-8")
    if trusted_version:
        (state_dir / "trusted-root.json").write_text(json.dumps(
            {"version": trusted_version, "metadata_sha256": "0" * 64}), encoding="utf-8")
    monkeypatch.setenv("UPDATE_ROOT_METADATA", str(tmp_path / "root.json"))
    monkeypatch.setenv("UPDATE_ROOT_KEYS", str(root_keys))
    monkeypatch.setenv("UPDATE_PUBLIC_KEY", str(keyring))
    signed_update_server.publish("0.0.0.2", _release_package(tmp_path, "0.0.0.2"),
                                 key_id="release-1")
    before = _health(root)
    request = requests / ("7" * 32 + ".json")
    request.write_text(json.dumps({"request_id": "7" * 32, "action": "update",
                                   "target_version": "0.0.0.2"}), encoding="utf-8")

    assert agent.process_request(root, state_dir, request, skip_drain=True) is False

    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED" and status["request_id"] == "7" * 32
    assert message in status["message"].lower(), (dimension, status["message"])
    assert _active_version(root) == "0.0.0.1"
    assert not (root / "releases" / "0.0.0.2").exists()
    assert not (state_dir / "packages").exists() or not any((state_dir / "packages").iterdir())
    assert _health(root) == before, f"refused {dimension} release still restarted the runtime"


@pytest.mark.parametrize(("dimension", "version", "fields", "message"), [
    ("expiry", "0.0.0.2", {"issued_at": "2020-01-01T00:00:00Z",
                           "expires_at": "2020-01-02T00:00:00Z"}, "expired"),
    ("channel", "0.0.0.2", {"channel": "beta"}, "channel"),
    ("compatibility", "0.0.0.2", {"min_version": "9.0.0.0"}, "upgrade path"),
    ("downgrade", "0.0.0.0", {}, "downgrade"),
    ("cross_origin", "0.0.0.2", {"package_url": "https://evil.example/vertep-0.0.0.2.tar.gz"},
     "outside"),
], ids=["expiry", "channel", "compatibility", "downgrade", "cross_origin"])
def test_validly_signed_but_untrusted_release_is_refused_before_any_host_mutation(
        agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server,
        dimension, version, fields, message):
    """#394 B2: the update executor entry refuses each trust-matrix row of a
    correctly signed manifest; release, packages and runtime stay unchanged."""
    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    signed_update_server.publish(version, _release_package(tmp_path, version), **fields)
    before = _health(root)
    request = requests / ("8" * 32 + ".json")
    request.write_text(json.dumps({"request_id": "8" * 32, "action": "update",
                                   "target_version": version}), encoding="utf-8")

    assert agent.process_request(root, state_dir, request, skip_drain=True) is False

    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED" and status["request_id"] == "8" * 32
    assert message in status["message"].lower(), (dimension, status["message"])
    assert _active_version(root) == "0.0.0.1"
    assert not (root / "releases" / version).exists()
    assert not (state_dir / "packages").exists() or not any((state_dir / "packages").iterdir())
    assert _health(root) == before, f"refused {dimension} release still restarted the runtime"


_WRITE_LIMITED_AGENT = r'''
import importlib.util, json, resource, signal, sys
from pathlib import Path
agent_path, root, state_dir, request, limit = sys.argv[1:6]
# A real OS write failure: every file this process writes is capped, so storing the
# downloaded package fails like a full disk (EFBIG instead of ENOSPC).
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (int(limit), int(limit)))
spec = importlib.util.spec_from_file_location("update_agent_limited", agent_path)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
print(json.dumps({"result": agent.process_request(Path(root), Path(state_dir), Path(request),
                                                   skip_drain=True)}))
'''


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_FSIZE is a POSIX limit")
def test_a_write_failure_while_storing_the_package_is_journaled_and_a_retry_succeeds(
        agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server):
    """#401 F2: the update executor itself, fed by the signed HTTPS origin, cannot store
    the downloaded package (a real OS write failure, as on a full disk). Nothing is
    applied, the failure is correlated with the request in status and the append-only
    journal, and the same release is applied by a retry once writing works again."""
    import subprocess

    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    package = _release_package(tmp_path, "0.0.0.2")
    payload = tmp_path / "payload-0.0.0.2"
    (payload / "bulk.bin").write_bytes(os.urandom(512 * 1024))  # larger than the write cap
    with tarfile.open(package, "w:gz") as archive:
        archive.add(payload, arcname=payload.name)
    signed_update_server.publish("0.0.0.2", package)
    before = _health(root)

    request_id = "c" * 32
    request = requests / f"{request_id}.json"
    request.write_text(json.dumps({"request_id": request_id, "action": "update",
                                   "target_version": "0.0.0.2"}), encoding="utf-8")
    child = subprocess.run(
        [sys.executable, "-c", _WRITE_LIMITED_AGENT, str(ROOT / "scripts" / "update-agent.py"),
         str(root), str(state_dir), str(request), str(256 * 1024)],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=300)
    assert json.loads(child.stdout.strip().splitlines()[-1]) == {"result": False}, child.stderr[-2000:]

    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED" and status["request_id"] == request_id, status
    assert "file too large" in status["message"].lower() or "space" in status["message"].lower(), status
    journal = _journal(state_dir)
    assert any(event.get("operation_id") == request_id and event.get("state") == "FAILED"
               for event in journal), journal[-3:]
    assert _active_version(root) == "0.0.0.1"
    assert not (root / "releases" / "0.0.0.2").exists()
    assert _health(root) == before, "a failed download still restarted the runtime"
    failed_journal = list(journal)

    retry_id = "e" * 32
    retry = requests / f"{retry_id}.json"
    retry.write_text(json.dumps({"request_id": retry_id, "action": "update",
                                 "target_version": "0.0.0.2"}), encoding="utf-8")
    assert agent.process_request(root, state_dir, retry, skip_drain=True) is True
    assert _active_version(root) == "0.0.0.2"
    journal = _journal(state_dir)
    assert journal[:len(failed_journal)] == failed_journal, "the append-only journal was rewritten"
    assert any(event.get("operation_id") == retry_id for event in journal[len(failed_journal):])


_FSYNC_FAULT_AGENT = r'''
import errno, importlib.util, json, os, sys
from pathlib import Path
agent_path, root, state_dir, request = sys.argv[1:5]
real_fsync, calls = os.fsync, {"n": 0}
def failing_fsync(fd):
    # The storage reports an I/O error on the durable write of the accepted release
    # sequence (``trusted-release.json``), before anything is downloaded or applied.
    try:
        target = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        target = ""
    if "trusted-release" in target:
        calls["n"] += 1
        raise OSError(errno.EIO, "Input/output error")
    return real_fsync(fd)
os.fsync = failing_fsync
spec = importlib.util.spec_from_file_location("update_agent_fsync", agent_path)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
print(json.dumps({"result": agent.process_request(Path(root), Path(state_dir), Path(request),
                                                   skip_drain=True), "fsync_calls": calls["n"]}))
'''


@pytest.mark.skipif(sys.platform == "win32", reason="targets the faulty file through /proc")
def test_an_fsync_fault_stops_the_update_with_a_journaled_outcome_and_a_retry_succeeds(
        agent, disposable_deployment, tmp_path, monkeypatch, signed_update_server):
    """#401 F2: the storage answers an I/O error on a durable write of the update executor
    (fault injected at ``os.fsync``). The update stops before anything is applied, the
    outcome is correlated with the request in the append-only journal, and a retry of
    the same release succeeds once the storage is healthy again."""
    import subprocess

    root = disposable_deployment(tmp_path / "appliance", "0.0.0.1")
    state_dir = tmp_path / "state"
    requests = state_dir / "requests"
    requests.mkdir(parents=True)
    _prepare(root, state_dir, monkeypatch)
    signed_update_server.publish("0.0.0.2", _release_package(tmp_path, "0.0.0.2"))
    before = _health(root)

    request_id = "f" * 32
    request = requests / f"{request_id}.json"
    request.write_text(json.dumps({"request_id": request_id, "action": "update",
                                   "target_version": "0.0.0.2"}), encoding="utf-8")
    child = subprocess.run(
        [sys.executable, "-c", _FSYNC_FAULT_AGENT, str(ROOT / "scripts" / "update-agent.py"),
         str(root), str(state_dir), str(request)],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=300)
    assert child.stdout.strip(), child.stderr[-3000:]
    outcome = json.loads(child.stdout.strip().splitlines()[-1])
    assert outcome["result"] is False and outcome["fsync_calls"] >= 1, (outcome, child.stderr[-2000:])

    status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED" and status["request_id"] == request_id, status
    journal = _journal(state_dir)
    assert any(event.get("operation_id") == request_id and event.get("phase") == "OUTCOME"
               and event.get("state") == "FAILED" for event in journal), journal[-3:]
    assert _active_version(root) == "0.0.0.1"
    assert not (root / "releases" / "0.0.0.2").exists()
    assert _health(root) == before

    retry_id = "9" * 32
    retry = requests / f"{retry_id}.json"
    retry.write_text(json.dumps({"request_id": retry_id, "action": "update",
                                 "target_version": "0.0.0.2"}), encoding="utf-8")
    assert agent.process_request(root, state_dir, retry, skip_drain=True) is True
    assert _active_version(root) == "0.0.0.2"
    assert any(event.get("operation_id") == retry_id and event.get("state") == "SUCCEEDED"
               for event in _journal(state_dir))
