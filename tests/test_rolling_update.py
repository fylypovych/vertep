from core.rolling_update import cancel_rollout, promote_rollout, reconcile_rollout, rollout_status, start_rollout


def test_rollout_updates_one_node_at_a_time_and_rolls_back_on_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    workers = {
        "gpu-1": {"node_name": "gpu-1", "status": "READY", "version": "1.0.0",
                  "current_task": None, "self_test": {"status": "PASSED"}},
        "gpu-2": {"node_name": "gpu-2", "status": "READY", "version": "1.0.0",
                  "current_task": None, "self_test": {"status": "PASSED"}},
    }
    # workers-first order sorts to ["gpu-1", "gpu-2"]
    start_rollout("1.1.0", ["gpu-2", "gpu-1"])
    state = reconcile_rollout(workers)
    assert state["max_unavailable"] == 1
    assert workers["gpu-1"]["desired_state"] == "UPDATING"
    assert "desired_state" not in workers["gpu-2"]

    # Use future timestamp so checked_at > phase_started_at when SELF_TESTING starts
    future_checked_at = "2099-12-31T23:59:59+00:00"
    workers["gpu-1"].update({"version": "1.1.0", "status": "READY",
                             "self_test": {"status": "PASSED", "checked_at": future_checked_at}})
    # Advance gpu-1 through SELF_TESTING → READY (3+ reconciles after update)
    reconcile_rollout(workers)  # DRAINING → UPDATING
    reconcile_rollout(workers)  # UPDATING → SELF_TESTING (phase_started set)
    reconcile_rollout(workers)  # SELF_TESTING → READY (checked_at > phase_started)
    # Now advance to next node (gpu-2)
    reconcile_rollout(workers)  # PENDING → DRAINING
    reconcile_rollout(workers)  # DRAINING → UPDATING (desired_state set)
    state = reconcile_rollout(workers)  # UPDATING (one more to be sure)
    assert workers["gpu-2"]["desired_state"] == "UPDATING"
    workers["gpu-2"]["status"] = "ERROR"
    state = reconcile_rollout(workers)
    assert state["state"] == "ROLLING_BACK"
    assert state["nodes"][1]["phase"] == "FAILED"
    assert workers["gpu-1"]["desired_state"] == "ROLLBACK"
    workers["gpu-1"].update({"version": "1.0.0", "status": "READY",
                              "self_test": {"status": "PASSED", "checked_at": future_checked_at}})
    assert reconcile_rollout(workers)["state"] == "ROLLED_BACK"


def test_canary_waits_for_explicit_promotion(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    workers = {
        "gpu-2": {"node_name": "gpu-2", "status": "READY", "version": "1.0.0",
                  "current_task": None, "self_test": {"status": "PASSED"}},
        "gpu-1": {"node_name": "gpu-1", "status": "READY", "version": "1.0.0",
                  "current_task": None, "self_test": {"status": "PASSED"}},
    }
    state = start_rollout("1.1.0", ["gpu-2", "gpu-1"], order="custom", canary=True)
    assert state["nodes"][0]["node_id"] == "gpu-2"
    reconcile_rollout(workers)  # PENDING -> DRAINING
    reconcile_rollout(workers)  # DRAINING -> UPDATING
    workers["gpu-2"]["version"] = "1.1.0"
    reconcile_rollout(workers)  # UPDATING -> SELF_TESTING (phase_started set)
    status = rollout_status()
    gpu2_node = next(n for n in status["nodes"] if n["node_id"] == "gpu-2")
    assert gpu2_node["phase"] == "SELF_TESTING"
    phase_started = gpu2_node["phase_started_at"]
    
    # Use future timestamp so checked_at > phase_started_at
    future_checked_at = "2099-12-31T23:59:59+00:00"
    workers["gpu-2"].update({"version": "1.1.0", "status": "READY",
                              "self_test": {"status": "PASSED", "checked_at": future_checked_at}})
    reconcile_rollout(workers)  # SELF_TESTING -> READY
    assert reconcile_rollout(workers)["state"] == "AWAITING_PROMOTION"
    assert "desired_state" not in workers["gpu-1"]
    assert promote_rollout()["state"] == "RUNNING"
    reconcile_rollout(workers)
    assert workers["gpu-1"]["desired_state"] == "UPDATING"


def test_rollout_rejects_parallel_operation(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    start_rollout("2.0.0", ["node-1"])
    try:
        start_rollout("2.1.0", ["node-2"])
    except RuntimeError as error:
        assert "already running" in str(error)
    else:
        raise AssertionError("parallel rolling update was accepted")

# ── self-test binding to target version/time (Issue #53, #15) ───────────────


def test_stale_self_test_does_not_approve_node(monkeypatch, tmp_path):
    """A PASSED self-test with checked_at BEFORE phase_started_at must not promote to READY."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    start_rollout("2.0.0", ["gpu-1"])
    workers = {
        "gpu-1": {"node_name": "gpu-1", "status": "FREE", "version": "1.0.0",
                  "current_task": None},
    }
    reconcile_rollout(workers)  # DRAINING → UPDATING
    # Simulate worker reached target version
    workers["gpu-1"]["version"] = "2.0.0"
    reconcile_rollout(workers)  # UPDATING → SELF_TESTING
    status = rollout_status()
    gpu1_node = next(n for n in status["nodes"] if n["node_id"] == "gpu-1")
    assert gpu1_node["phase"] == "SELF_TESTING"
    phase_started = gpu1_node["phase_started_at"]
    # Inject a PASSED test with checked_at BEFORE phase_started (stale)
    workers["gpu-1"]["self_test"] = {
        "status": "PASSED", "role": "gpu",
        "checked_at": "2000-01-01T00:00:00+00:00",  # way before phase_started
    }
    reconcile_rollout(workers)
    status = rollout_status()
    gpu1_node = next(n for n in status["nodes"] if n["node_id"] == "gpu-1")
    # Must NOT have promoted to READY because the test is stale
    assert gpu1_node["phase"] == "SELF_TESTING"


def test_fresh_self_test_approves_node(monkeypatch, tmp_path):
    """A PASSED self-test with checked_at AFTER phase_started_at promotes to READY."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    start_rollout("2.0.0", ["gpu-1"])
    workers = {
        "gpu-1": {"node_name": "gpu-1", "status": "FREE", "version": "1.0.0",
                  "current_task": None},
    }
    reconcile_rollout(workers)  # DRAINING → UPDATING
    workers["gpu-1"]["version"] = "2.0.0"
    reconcile_rollout(workers)  # UPDATING → SELF_TESTING
    status = rollout_status()
    gpu1_node = next(n for n in status["nodes"] if n["node_id"] == "gpu-1")
    # Inject a PASSED test with checked_at AFTER phase_started (fresh)
    workers["gpu-1"]["self_test"] = {
        "status": "PASSED", "role": "gpu",
        "checked_at": "2099-12-31T23:59:59+00:00",
    }
    reconcile_rollout(workers)
    status = rollout_status()
    gpu1_node = next(n for n in status["nodes"] if n["node_id"] == "gpu-1")
    assert gpu1_node["phase"] == "READY"


def test_wrong_version_self_test_does_not_approve(monkeypatch, tmp_path):
    """PASSED test + fresh time but wrong version must not promote (stays in UPDATING)."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    start_rollout("2.0.0", ["gpu-1"])
    workers = {
        "gpu-1": {"node_name": "gpu-1", "status": "FREE", "version": "1.0.0",
                  "current_task": None},
    }
    reconcile_rollout(workers)  # PENDING -> DRAINING
    reconcile_rollout(workers)  # DRAINING -> UPDATING
    # Worker is still at 1.0.0 (never updated to target 2.0.0)
    workers["gpu-1"]["self_test"] = {
        "status": "PASSED", "role": "gpu",
        "checked_at": "2099-12-31T23:59:59+00:00",
    }
    reconcile_rollout(workers)  # Should stay in UPDATING, not advance to SELF_TESTING
    status = rollout_status()
    gpu1_node = next(n for n in status["nodes"] if n["node_id"] == "gpu-1")
    assert gpu1_node["phase"] == "UPDATING"  # stuck in UPDATING because version mismatch


# ── cancel_rollout cleanup (Issue #53, #15) ────────────────────────────────


def test_cancel_rollout_clears_worker_update_commands(monkeypatch, tmp_path):
    """cancel_rollout + reconcile must clean desired_state/update_target_version."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    start_rollout("2.0.0", ["gpu-1", "gpu-2"])
    workers = {
        "gpu-1": {"node_name": "gpu-1", "status": "BUSY", "version": "1.0.0",
                  "desired_state": "UPDATING", "update_target_version": "2.0.0",
                  "update_operation_id": "op-1"},
        "gpu-2": {"node_name": "gpu-2", "status": "READY", "version": "1.0.0"},
    }
    # Simulate a node in UPDATING phase
    from core.rolling_update import reconcile_rollout
    reconcile_rollout(workers)  # advance to DRAINING/UPDATING
    # Now cancel
    cancel_rollout()
    # Reconcile should clean up residual commands on affected workers
    reconcile_rollout(workers)
    gpu1 = workers["gpu-1"]
    assert "desired_state" not in gpu1
    assert "update_target_version" not in gpu1
    assert "update_operation_id" not in gpu1


def test_cancel_rollout_preserves_ready_nodes(monkeypatch, tmp_path):
    """Nodes already in READY/FAILED/ROLLED_BACK should not be touched."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    start_rollout("2.0.0", ["node-1", "node-2"])
    # Provide a live worker so DRAINING→UPDATING transition happens
    workers = {
        "node-1": {"node_name": "node-1", "status": "FREE", "version": "1.0.0",
                   "current_task": None},
        "node-2": {"node_name": "node-2", "status": "FREE", "version": "1.0.0"},
    }
    reconcile_rollout(workers)
    status = rollout_status()
    # node-1 should be in UPDATING or SELF_TESTING by now
    cancel_rollout()
    status = rollout_status()
    assert status["state"] == "CANCELLED"
    for node in status["nodes"]:
        assert node["phase"] == "CANCELLED"


def test_cancel_idempotent(monkeypatch, tmp_path):
    """Calling cancel_rollout on already-cancelled rollout is safe."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    start_rollout("2.0.0", ["node-1"])
    cancel_rollout()
    result = cancel_rollout()
    assert result["state"] == "CANCELLED"


def test_cleanup_cancelled_workers_no_op_when_not_cancelled():
    """_cleanup_cancelled_workers is a no-op for non-CANCELLED rollouts."""
    from core.rolling_update import _cleanup_cancelled_workers
    workers = {"gpu-1": {"desired_state": "UPDATING", "update_target_version": "2.0.0"}}
    _cleanup_cancelled_workers(workers, {"state": "RUNNING", "nodes": []})
    assert workers["gpu-1"]["desired_state"] == "UPDATING"

