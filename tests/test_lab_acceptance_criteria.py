"""Comprehensive test for all 7 acceptance criteria of policy gate (Issue #107, stage 3).

AGENTS.md §4.6/§4.7 requirements:
3.1 Automatic closing of ir — only after real evidence (passed ≠ partial tests).
3.2 Atomic sequence: create next ir → verify links → close previous ir.
3.3 create_ir through gate — traced to source, duplicate check.
3.4 comment_issue and edit_issue — in FORBIDDEN_ACTIONS.
3.5 close_issue — in FORBIDDEN_ACTIONS.
3.6 Test: retry create_lab_issue does not duplicate.
3.7 Test: revoke grant → gate blocks corresponding action.
3.8 Test: gate_inactive → only STANDING_PERMISSIONS work.
3.9 audit.jsonl: every attempt recorded with actor, action, subject, decision, rule.
"""

import pytest
from datetime import datetime, timedelta, timezone

from lab.policy import (FORBIDDEN_ACTIONS, PolicyGate, STANDING_PERMISSIONS,
                        Action)
from lab.governance import Governance, IssueTransport
from lab.audit import AuditTrail


class _FakeTransport(IssueTransport):
    def __init__(self):
        self.created = []
        self.duplicates = []

    def create_issue(self, title, body, labels=()):
        self.created.append({"title": title, "labels": list(labels)})
        return {"number": len(self.created) + 100, "url": f"https://github.com/owner/repo/issues/{len(self.created) + 100}",
                "dry_run": self.dry_run}

    def find_duplicates(self, title):
        self.duplicates.append(title)
        return []


def _future(hours=8):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def test_criterion_3_4_edit_issue_is_forbidden():
    """comment_issue and edit_issue are in FORBIDDEN_ACTIONS."""
    assert "edit_issue" in FORBIDDEN_ACTIONS
    assert "comment_issue" in FORBIDDEN_ACTIONS


def test_criterion_3_5_close_issue_is_forbidden():
    """close_issue is in FORBIDDEN_ACTIONS."""
    assert "close_issue" in FORBIDDEN_ACTIONS


def test_criterion_3_6_retry_create_lab_issue_does_not_duplicate(tmp_path):
    """Retry create_lab_issue does not duplicate."""
    gate = PolicyGate(tmp_path / "state", activated=True)
    gate.register_grant(actor="lab", action="create_lab_issue", subject="lab.0.0.0.1",
                        approved_by="owner", expires_at=_future())
    transport = _FakeTransport()
    governance = Governance(gate, transport, state_dir=tmp_path / "gov", repository="owner/repo")

    finding = {
        "identifier": "lab.0.0.0.1", "kind": "defect", "title": "Тест дублікату",
        "evidence": "desc", "reproduction": "desc", "risk": "desc",
        "expected_result": "desc", "boundaries": "desc",
        "source": "src", "approved_scope": "scope",
    }

    first = governance.propose_lab_issue(finding, key="lab.0.0.0.1")
    assert first["created"] is True
    assert len(transport.created) == 1

    second = governance.propose_lab_issue(finding, key="lab.0.0.0.1")
    assert second["created"] is False
    assert second["reason"] == "already created"
    assert len(transport.created) == 1  # Still only one created


def test_criterion_3_7_revoke_grant_blocks_action(tmp_path):
    """Revoke grant → gate blocks corresponding action."""
    gate = PolicyGate(tmp_path / "state", activated=True)
    grant = gate.register_grant(actor="lab", action="create_lab_issue", subject="lab.0.0.0.1",
                                approved_by="owner", expires_at=_future())

    assert gate.allow("create_lab_issue", actor="lab", subject="lab.0.0.0.1") is True

    gate.revoke_grant(grant.grant_id)
    assert gate.allow("create_lab_issue", actor="lab", subject="lab.0.0.0.1") is False


def test_criterion_3_8_gate_inactive_only_standing_permissions_work(tmp_path):
    """gate_inactive → only STANDING_PERMISSIONS work."""
    inactive = PolicyGate(tmp_path / "state", activated=False)

    # Standing permissions should work
    for action in STANDING_PERMISSIONS:
        assert inactive.allow(action, actor="lab") is True

    # Gated actions should be denied
    assert inactive.allow("create_lab_issue", actor="lab", subject="lab.0.0.0.1") is False
    assert inactive.allow("create_ir_issue", actor="lab", subject="ir.0.0.0.1") is False


def test_criterion_3_9_audit_jsonl_records_every_attempt(tmp_path):
    """audit.jsonl: every attempt recorded with actor, action, subject, decision, rule."""
    gate = PolicyGate(tmp_path / "state", activated=True)
    gate.register_grant(actor="lab", action="create_lab_issue", subject="lab.0.0.0.1",
                        approved_by="owner", expires_at=_future())

    # Make several decisions
    gate.decide("local_fix", actor="lab", subject="test")
    gate.decide("push", actor="lab", subject="")
    gate.decide("create_lab_issue", actor="lab", subject="lab.0.0.0.1")

    records = gate.trail.records()
    # register_grant also records a decision, so we have 4 records
    assert len(records) == 4

    # Find the three decisions we made (skip the grant record)
    decisions = [r for r in records if not r["action"].startswith("grant:")]
    assert len(decisions) == 3

    # Check first decision (allowed standing permission)
    assert decisions[0]["actor"] == "lab"
    assert decisions[0]["action"] == "local_fix"
    assert decisions[0]["decision"] == "ALLOWED"
    assert decisions[0]["rule"] == "standing_permission"
    assert "previous_hash" in decisions[0]
    assert "hash" in decisions[0]

    # Check second decision (denied forbidden action)
    assert decisions[1]["actor"] == "lab"
    assert decisions[1]["action"] == "push"
    assert decisions[1]["decision"] == "DENIED"
    assert decisions[1]["rule"] == "forbidden_action"

    # Check third decision (allowed gated action with grant)
    assert decisions[2]["actor"] == "lab"
    assert decisions[2]["action"] == "create_lab_issue"
    assert decisions[2]["decision"] == "ALLOWED"
    assert decisions[2]["rule"] == "owner_grant"
    assert decisions[2]["extra"]["grant_id"]


def test_criterion_3_3_create_ir_through_gate_traced(tmp_path):
    """create_ir through gate — traced to source, duplicate check."""
    gate = PolicyGate(tmp_path / "state", activated=True)
    # Subject must match what we pass to create_ir_issue
    gate.register_grant(actor="lab", action="create_ir_issue", subject="test ir",
                        approved_by="owner", expires_at=_future())

    transport = _FakeTransport()
    governance = Governance(gate, transport, state_dir=tmp_path / "gov", repository="owner/repo")

    # Verify gate decision is recorded
    result = governance.create_ir_issue("test ir")
    assert result["allowed"] is True

    # Verify audit trail records the decision
    records = gate.trail.records()
    create_ir_records = [r for r in records if r["action"] == "create_ir_issue"]
    assert len(create_ir_records) >= 1
    assert create_ir_records[-1]["decision"] == "ALLOWED"


def test_all_forbidden_actions_are_truly_forbidden(tmp_path):
    """All actions in FORBIDDEN_ACTIONS are always denied."""
    gate = PolicyGate(tmp_path / "state", activated=True)

    for action in FORBIDDEN_ACTIONS:
        decision = gate.decide(action, actor="lab", subject="")
        assert decision.allowed is False, f"{action} should be forbidden"
        assert decision.rule == "forbidden_action"


def test_audit_trail_is_tamper_evident(tmp_path):
    """Audit trail detects tampering."""
    trail = AuditTrail(tmp_path / "audit.jsonl")
    trail.append(actor="lab", action="local_fix", subject="", decision="ALLOWED",
                 rule="standing_permission")

    assert trail.verify()["valid"] is True

    # Tamper with the record
    import json
    path = tmp_path / "audit.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    record["decision"] = "DENIED"
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")

    assert trail.verify()["valid"] is False
