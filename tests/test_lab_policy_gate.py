"""Policy gate proofs for the autonomous laboratory (Issue #107).

These tests encode the governance rules from ``AGENTS.md`` §4.6/§4.7 and the
issue: deny by default, no autonomous publication, only new ``lab`` issues,
scoped and expiring grants, fail-closed while the gate is inactive, and a
tamper-evident audit trail that a restart cannot rewrite.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from lab.audit import AuditTrail
from lab.policy import (FORBIDDEN_ACTIONS, STANDING_PERMISSIONS, PolicyDenied, PolicyGate,
                        validate_lab_finding)


@pytest.fixture()
def gate(tmp_path):
    return PolicyGate(tmp_path / "state", activated=True)


def _future(hours: int = 8) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_unknown_action_is_denied(gate):
    decision = gate.decide("delete_everything", actor="lab")
    assert decision.allowed is False
    assert decision.rule == "unknown_action"


def test_standing_permissions_are_allowed_inside_the_isolated_copy(gate):
    for action in sorted(STANDING_PERMISSIONS):
        assert gate.allow(action, actor="lab") is True


@pytest.mark.parametrize("action", sorted(FORBIDDEN_ACTIONS))
def test_publication_and_production_writes_are_never_allowed(gate, action):
    decision = gate.decide(action, actor="lab")
    assert decision.allowed is False
    assert decision.rule == "forbidden_action"


@pytest.mark.parametrize("action", ["push", "release", "create_branch", "create_pr"])
def test_publication_cannot_be_granted_at_all(gate, action):
    with pytest.raises(ValueError):
        gate.register_grant(actor="lab", action=action, subject="", approved_by="owner",
                            expires_at=_future())


def test_no_autonomous_power_before_the_gate_is_activated(tmp_path):
    inactive = PolicyGate(tmp_path / "state", activated=False)
    decision = inactive.decide("create_lab_issue", actor="lab", subject="lab.0.0.0.1")
    assert decision.allowed is False
    assert decision.rule == "gate_inactive"


def test_activation_switch_can_be_overridden_by_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_POLICY_GATE_ACTIVE", "true")
    assert PolicyGate(tmp_path / "state", activated=False).activated is True


# -- grants ---------------------------------------------------------------
def test_gated_action_requires_a_matching_active_grant(gate):
    assert gate.allow("create_lab_issue", actor="lab", subject="lab.0.0.0.1") is False
    gate.register_grant(actor="lab", action="create_lab_issue", subject="lab.0.0.0.1",
                        approved_by="owner", expires_at=_future())
    assert gate.allow("create_lab_issue", actor="lab", subject="lab.0.0.0.1") is True


def test_grant_is_scoped_to_actor_action_and_subject(gate):
    gate.register_grant(actor="lab", action="create_lab_issue", subject="lab.0.0.0.1",
                        approved_by="owner", expires_at=_future())
    assert gate.allow("create_lab_issue", actor="other", subject="lab.0.0.0.1") is False
    assert gate.allow("create_lab_issue", actor="lab", subject="lab.0.0.0.2") is False
    assert gate.allow("close_issue", actor="lab", subject="lab.0.0.0.1") is False


def test_expired_grant_denies(gate):
    gate.register_grant(actor="lab", action="create_lab_issue", subject="lab.0.0.0.1",
                        approved_by="owner", expires_at=_past())
    assert gate.allow("create_lab_issue", actor="lab", subject="lab.0.0.0.1") is False


def test_revoked_grant_blocks_immediately(gate):
    grant = gate.register_grant(actor="lab", action="create_lab_issue",
                                subject="lab.0.0.0.1", approved_by="owner",
                                expires_at=_future())
    assert gate.allow("create_lab_issue", actor="lab", subject="lab.0.0.0.1") is True
    assert gate.revoke_grant(grant.grant_id) is True
    assert gate.allow("create_lab_issue", actor="lab", subject="lab.0.0.0.1") is False


def test_grant_without_owner_identity_is_refused(gate):
    with pytest.raises(ValueError):
        gate.register_grant(actor="lab", action="create_lab_issue", subject="lab.0.0.0.1",
                            approved_by="", expires_at=_future())


def test_grant_for_a_non_gated_action_is_refused(gate):
    with pytest.raises(ValueError):
        gate.register_grant(actor="lab", action="local_fix", subject="",
                            approved_by="owner", expires_at=_future())


def test_require_raises_with_the_decision(gate):
    with pytest.raises(PolicyDenied) as error:
        gate.require("create_i_issue", actor="lab", subject="i.0.0.0.1")
    assert error.value.decision.allowed is False
    assert error.value.decision.rule == "no_active_grant"


def test_grant_survives_a_restart(tmp_path):
    first = PolicyGate(tmp_path / "state", activated=True)
    first.register_grant(actor="lab", action="create_lab_issue", subject="lab.0.0.0.1",
                         approved_by="owner", expires_at=_future())
    restarted = PolicyGate(tmp_path / "state", activated=True)
    assert restarted.allow("create_lab_issue", actor="lab", subject="lab.0.0.0.1") is True
    assert restarted.grants()[0].approved_by == "owner"


# -- audit trail ----------------------------------------------------------
def test_every_decision_is_audited(gate):
    gate.allow("local_fix", actor="lab")
    gate.allow("push", actor="lab")
    records = gate.trail.records()
    assert [record["action"] for record in records] == ["local_fix", "push"]
    assert [record["decision"] for record in records] == ["ALLOWED", "DENIED"]


def test_audit_trail_detects_tampering(tmp_path):
    trail = AuditTrail(tmp_path / "audit.jsonl")
    trail.append(actor="lab", action="local_fix", subject="", decision="DENIED",
                 rule="forbidden_action")
    assert trail.verify()["valid"] is True

    path = tmp_path / "audit.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    record["decision"] = "ALLOWED"
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    report = trail.verify()
    assert report["valid"] is False
    assert report["problems"]


def test_audit_trail_detects_a_removed_record(tmp_path):
    trail = AuditTrail(tmp_path / "audit.jsonl")
    for index in range(3):
        trail.append(actor="lab", action=f"a{index}", subject="", decision="ALLOWED",
                     rule="standing_permission")
    path = tmp_path / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    assert trail.verify()["valid"] is False


def test_audit_trail_never_stores_secret_values(tmp_path):
    trail = AuditTrail(tmp_path / "audit.jsonl")
    trail.append(actor="lab", action="request_gpu_lease", subject="qwen",
                 decision="ALLOWED", rule="standing_permission",
                 extra={"token": "super-secret-value"})
    assert "super-secret-value" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


# -- lab finding validation ----------------------------------------------
def _finding(**overrides):
    finding = {
        "identifier": "lab.0.0.0.1", "kind": "defect",
        "title": "Компіляція падає на модулі X",
        "evidence": "python -m compileall -q core → SyntaxError у core/x.py:42",
        "reproduction": "python -m compileall -q core",
        "risk": "реліз блокується",
        "expected_result": "compileall проходить без помилок",
        "boundaries": "лише виправлення синтаксису, без зміни поведінки",
        "source": "tests/test_release_qualification.py",
        "approved_scope": "AGENTS.md §11 — тести перед релізом",
    }
    finding.update(overrides)
    return finding


def test_valid_finding_passes_validation():
    assert validate_lab_finding(_finding())["valid"] is True


def test_finding_without_evidence_is_rejected():
    report = validate_lab_finding(_finding(evidence=""))
    assert report["valid"] is False
    assert "missing evidence" in report["problems"]


def test_product_idea_is_not_a_lab_finding():
    report = validate_lab_finding(_finding(title="Ідея: додати нову платформу"))
    assert report["valid"] is False
    assert any("product ideas" in problem for problem in report["problems"])


def test_finding_without_approved_scope_is_rejected():
    report = validate_lab_finding(_finding(approved_scope=""))
    assert "missing approved_scope" in report["problems"]


def test_finding_kind_must_be_a_confirmed_problem():
    assert validate_lab_finding(_finding(kind="enhancement"))["valid"] is False


def test_bad_identifier_is_rejected():
    report = validate_lab_finding(_finding(identifier="lab-1"))
    assert any("identifier" in problem for problem in report["problems"])


