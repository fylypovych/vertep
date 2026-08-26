from core.rolling_update import reconcile_rollout, rollout_status, start_rollout


def test_rollout_updates_one_node_at_a_time_and_stops_on_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    workers = {
        "gpu-1": {"node_name": "gpu-1", "status": "READY", "version": "1.0.0",
                  "current_task": None, "self_test": {"status": "PASSED"}},
        "gpu-2": {"node_name": "gpu-2", "status": "READY", "version": "1.0.0",
                  "current_task": None, "self_test": {"status": "PASSED"}},
    }
    start_rollout("1.1.0", ["gpu-2", "gpu-1"])
    state = reconcile_rollout(workers)
    assert state["max_unavailable"] == 1
    assert workers["gpu-1"]["desired_state"] == "UPDATING"
    assert "desired_state" not in workers["gpu-2"]

    workers["gpu-1"].update({"version": "1.1.0", "status": "READY",
                             "self_test": {"status": "PASSED"}})
    reconcile_rollout(workers)
    reconcile_rollout(workers)
    state = reconcile_rollout(workers)
    assert workers["gpu-2"]["desired_state"] == "UPDATING"
    workers["gpu-2"]["status"] = "ERROR"
    state = reconcile_rollout(workers)
    assert state["state"] == "FAILED"
    assert state["nodes"][1]["phase"] == "FAILED"
    assert rollout_status()["state"] == "FAILED"


def test_rollout_rejects_parallel_operation(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    start_rollout("2.0.0", ["node-1"])
    try:
        start_rollout("2.1.0", ["node-2"])
    except RuntimeError as error:
        assert "already running" in str(error)
    else:
        raise AssertionError("parallel rolling update was accepted")
