"""Temporary rights requests: durable demand, never a grant (Issue #250, L7).

The laboratory may *ask* the owner for a scoped, expiring right; the request is
persisted and audited, but no effect becomes available until the owner mints a
matching grant.
"""

from datetime import datetime, timedelta, timezone

import pytest

from lab.policy import FORBIDDEN_ACTIONS, GATED_ACTIONS, PolicyGate


def _gate(tmp_path, **kwargs) -> PolicyGate:
    return PolicyGate(tmp_path / "state", activated=True, **kwargs)


def test_request_is_persisted_but_grants_nothing(tmp_path):
    gate = _gate(tmp_path)
    record = gate.request_rights(actor="qa", action="create_lab_issue",
                                 subject="finding-1",
                                 reason="confirmed defect ready for the owner",
                                 ttl_hours=4)
    assert record["state"] == "PENDING"
    assert record["request_id"] == "r-0001"
    assert record["actor"] == "qa"
    assert record["action"] == "create_lab_issue"
    assert record["ttl_hours"] == 4

    stored = gate.rights_requests()
    assert len(stored) == 1
    assert stored[0] == record

    # the requested action stays denied until the owner approves it
    decision = gate.decide("create_lab_issue", actor="qa", subject="finding-1")
    assert not decision.allowed
    assert decision.rule == "no_active_grant"


def test_request_is_audited_without_any_secret(tmp_path):
    gate = _gate(tmp_path)
    gate.request_rights(actor="ai_gpu", action="standalone_verification",
                        subject="ir.0.0.0.1", reason="real stand is free")
    records = [r for r in gate.trail.records()
               if r.get("action") == "rights_request:standalone_verification"]
    assert records and records[-1]["decision"] == "REQUESTED"
    assert records[-1]["actor"] == "ai_gpu"
    assert records[-1]["subject"] == "ir.0.0.0.1"
    assert "real stand is free" in records[-1].get("reason", "")
    assert gate.trail.verify()["valid"]


def test_request_identifiers_are_sequential(tmp_path):
    gate = _gate(tmp_path)
    first = gate.request_rights(actor="qa", action="create_lab_issue", reason="one")
    second = gate.request_rights(actor="qa", action="create_lab_issue", reason="two")
    assert first["request_id"] == "r-0001"
    assert second["request_id"] == "r-0002"
    assert [r["reason"] for r in gate.rights_requests()] == ["one", "two"]


def test_requests_survive_a_gate_restart(tmp_path):
    gate = _gate(tmp_path)
    gate.request_rights(actor="backend", action="standalone_verification",
                        subject="lab-copy", reason="needs the owner")
    reloaded = _gate(tmp_path)
    stored = reloaded.rights_requests()
    assert len(stored) == 1
    assert stored[0]["actor"] == "backend"
    assert stored[0]["state"] == "PENDING"


def test_environment_and_scope_are_recorded_on_the_request(tmp_path):
    gate = _gate(tmp_path, environment="lab-vm", scope_version="scope-v2")
    record = gate.request_rights(actor="qa", action="create_lab_issue")
    assert record["environment"] == "lab-vm"
    assert record["scope_version"] == "scope-v2"


def test_environment_mismatched_grant_does_not_answer_the_request(tmp_path):
    gate = _gate(tmp_path, environment="lab-vm")
    gate.request_rights(actor="qa", action="create_lab_issue", subject="finding-1")
    # a grant minted for another environment must not unlock the effect
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    gate.register_grant(actor="qa", action="create_lab_issue", subject="finding-1",
                        approved_by="owner", expires_at=expires,
                        environment="lab")
    assert not gate.decide("create_lab_issue", actor="qa",
                           subject="finding-1").allowed


@pytest.mark.parametrize("action", sorted(FORBIDDEN_ACTIONS))
def test_forbidden_actions_can_never_be_requested(tmp_path, action):
    gate = _gate(tmp_path)
    with pytest.raises(ValueError, match="can never be requested"):
        gate.request_rights(actor="lab", action=action)
    assert gate.rights_requests() == []


def test_standing_actions_are_not_temporary_rights(tmp_path):
    gate = _gate(tmp_path)
    for action in ("read_state", "run_tests", "local_fix", "write_report",
                   "request_gpu_lease", "static_checks", "handoff"):
        with pytest.raises(ValueError, match="not a temporary right"):
            gate.request_rights(actor="qa", action=action)
    assert gate.rights_requests() == []


def test_only_gated_actions_may_be_requested(tmp_path):
    gate = _gate(tmp_path)
    for action in sorted(GATED_ACTIONS):
        record = gate.request_rights(actor="qa", action=action)
        assert record["action"] == action
    assert len(gate.rights_requests()) == len(GATED_ACTIONS)


def test_non_positive_ttl_is_refused(tmp_path):
    gate = _gate(tmp_path)
    for ttl in (0, -3):
        with pytest.raises(ValueError, match="ttl_hours must be positive"):
            gate.request_rights(actor="qa", action="create_lab_issue",
                                ttl_hours=ttl)
    assert gate.rights_requests() == []
