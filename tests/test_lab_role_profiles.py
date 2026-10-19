"""Test role profiles against policy gate (Issue #107, stage 2)."""

import pytest

from lab.policy import Action, PolicyGate, STANDING_PERMISSIONS, GATED_ACTIONS, FORBIDDEN_ACTIONS
from lab.workspace import Workspace
from pathlib import Path
import tempfile


# Expected permission sets per role
ROLE_PERMISSIONS = {
    "architect": {
        "standing": {"read_state", "static_checks", "write_report"},
        "gated": {"create_lab_issue"},
    },
    "backend": {
        "standing": {"read_state", "static_checks", "run_tests", "local_fix", "write_report"},
        "gated": {"create_lab_issue"},
    },
    "ai_gpu": {
        "standing": {"read_state", "static_checks", "request_gpu_lease", "write_report"},
        "gated": {"create_lab_issue"},
    },
    "frontend": {
        "standing": {"read_state", "static_checks", "write_report"},
        "gated": {"create_lab_issue"},
    },
    "devops": {
        "standing": {"read_state", "static_checks", "write_report"},
        "gated": {"create_lab_issue"},
    },
    "qa": {
        "standing": {"read_state", "static_checks", "run_tests", "local_fix", "write_report"},
        "gated": {"create_lab_issue"},
    },
    "security": {
        "standing": {"read_state", "static_checks", "write_report"},
        "gated": {"create_lab_issue"},
    },
    "reviewer": {
        "standing": {"read_state", "static_checks", "write_report"},
        "gated": set(),  # reviewer has NO gated actions
    },
}


def test_role_permissions_valid():
    """All role permissions must be subsets of the global sets."""
    for role, perms in ROLE_PERMISSIONS.items():
        # Standing permissions must be in STANDING_PERMISSIONS
        invalid_standing = perms["standing"] - STANDING_PERMISSIONS
        assert not invalid_standing, f"{role} has invalid standing permissions: {invalid_standing}"

        # Gated actions must be in GATED_ACTIONS
        invalid_gated = perms["gated"] - GATED_ACTIONS
        assert not invalid_gated, f"{role} has invalid gated actions: {invalid_gated}"

        # No forbidden actions anywhere
        combined = perms["standing"] | perms["gated"]
        forbidden = combined & FORBIDDEN_ACTIONS
        assert not forbidden, f"{role} has forbidden actions: {forbidden}"


def test_reviewer_has_no_local_fix():
    """Reviewer must not have local_fix to maintain independence."""
    assert "local_fix" not in ROLE_PERMISSIONS["reviewer"]["standing"]
    assert "local_fix" not in ROLE_PERMISSIONS["reviewer"]["gated"]


def test_qa_has_local_fix():
    """QA may write tests, so needs local_fix."""
    assert "local_fix" in ROLE_PERMISSIONS["qa"]["standing"]


def test_reviewer_has_no_gated_actions():
    """Reviewer is read-only: no gated actions at all."""
    assert ROLE_PERMISSIONS["reviewer"]["gated"] == set()


def test_ai_gpu_can_request_lease():
    """AI/GPU role must be able to request GPU lease."""
    assert "request_gpu_lease" in ROLE_PERMISSIONS["ai_gpu"]["standing"]


def test_backend_can_fix_code():
    """Backend role must be able to fix code."""
    assert "local_fix" in ROLE_PERMISSIONS["backend"]["standing"]


def test_gate_enforces_role_permissions():
    """Policy gate must deny actions not in role's standing permissions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        gate = PolicyGate(state_dir, activated=False)  # gate inactive

        # Standing permissions should work
        for action in STANDING_PERMISSIONS:
            decision = gate.decide(action, actor="lab", subject="")
            assert decision.allowed, f"Standing permission {action} should be allowed"

        # Gated actions should be denied without grant
        for action in GATED_ACTIONS:
            decision = gate.decide(action, actor="lab", subject="")
            assert not decision.allowed, f"Gated action {action} should be denied without grant"

        # Forbidden actions should always be denied
        for action in FORBIDDEN_ACTIONS:
            decision = gate.decide(action, actor="lab", subject="")
            assert not decision.allowed, f"Forbidden action {action} should always be denied"


def test_role_standing_permissions_allowed_without_grant():
    """All standing permissions should work without any grant."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        gate = PolicyGate(state_dir, activated=False)

        for role, perms in ROLE_PERMISSIONS.items():
            for action in perms["standing"]:
                decision = gate.decide(action, actor=role, subject="")
                assert decision.allowed, f"{role} standing permission {action} should work without grant"


def test_role_gated_actions_denied_without_grant():
    """All gated actions should be denied without grant."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        gate = PolicyGate(state_dir, activated=True)  # gate active

        for role, perms in ROLE_PERMISSIONS.items():
            for action in perms["gated"]:
                decision = gate.decide(action, actor=role, subject="")
                assert not decision.allowed, f"{role} gated action {action} should be denied without grant"


def test_gated_actions_allowed_with_grant():
    """Gated actions should be allowed with a matching grant."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        gate = PolicyGate(state_dir, activated=True)

        # Register a grant for create_lab_issue
        from datetime import datetime, timedelta, timezone
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        gate.register_grant(
            actor="backend",
            action="create_lab_issue",
            subject="test",
            approved_by="owner",
            expires_at=expires,
        )

        decision = gate.decide("create_lab_issue", actor="backend", subject="test")
        assert decision.allowed, "Gated action should be allowed with matching grant"
