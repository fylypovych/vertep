"""Role profiles come from `lab/role_profiles.md` and bind the gate (Issue #250).

Every expectation below is checked against the *real* parser
(`lab.roles.load_role_profiles`) reading the owner-approved markdown document,
and against the *real* `PolicyGate.decide` decisions for those roles — no
inline copy of the role table is trusted.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lab.policy import (FORBIDDEN_ACTIONS, GATED_ACTIONS, ROLE_PROFILES, ROLES,
                        STANDING_PERMISSIONS, PolicyGate)
from lab.roles import RoleProfilesError, load_role_profiles

EXPECTED_ROLES = {
    "architect": {"standing": {"read_state", "static_checks", "write_report"},
                  "gated": {"create_lab_issue"}},
    "backend": {"standing": {"read_state", "static_checks", "run_tests",
                              "local_fix", "write_report"},
                "gated": {"create_lab_issue"}},
    "ai_gpu": {"standing": {"read_state", "static_checks", "request_gpu_lease",
                             "write_report"},
               "gated": {"create_lab_issue"}},
    "frontend": {"standing": {"read_state", "static_checks", "write_report"},
                 "gated": {"create_lab_issue"}},
    "devops": {"standing": {"read_state", "static_checks", "write_report"},
               "gated": {"create_lab_issue"}},
    "qa": {"standing": {"read_state", "static_checks", "run_tests",
                         "local_fix", "write_report"},
           "gated": {"create_lab_issue"}},
    "security": {"standing": {"read_state", "static_checks", "write_report"},
                 "gated": {"create_lab_issue"}},
    "reviewer": {"standing": {"read_state", "static_checks", "write_report"},
                 "gated": set()},
}


def _gate(tmp_path) -> PolicyGate:
    return PolicyGate(tmp_path / "state", activated=True)


def _grant(gate, actor: str, action: str, subject: str):
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    gate.register_grant(actor=actor, action=action, subject=subject,
                        approved_by="owner", expires_at=expires)


def _write_profiles(tmp_path, body: str) -> Path:
    path = tmp_path / "role_profiles.md"
    path.write_text(body, encoding="utf-8")
    return path


def _load(path):
    return load_role_profiles(path, standing=STANDING_PERMISSIONS,
                              gated=GATED_ACTIONS, forbidden=FORBIDDEN_ACTIONS)


# -- the real document parses into the approved table ---------------------
def test_all_eight_approved_roles_are_parsed_from_the_document():
    assert set(ROLE_PROFILES) == set(EXPECTED_ROLES)
    assert set(ROLES) == set(EXPECTED_ROLES)


@pytest.mark.parametrize("role", sorted(EXPECTED_ROLES))
def test_profile_matches_the_approved_permission_table(role):
    profile = ROLE_PROFILES[role]
    expected = EXPECTED_ROLES[role]
    assert set(profile.standing) == expected["standing"]
    assert set(profile.gated) == expected["gated"]


@pytest.mark.parametrize("role", sorted(EXPECTED_ROLES))
def test_profile_permissions_are_defined_in_policy(role):
    profile = ROLE_PROFILES[role]
    assert not set(profile.standing) - set(STANDING_PERMISSIONS)
    assert not set(profile.gated) - set(GATED_ACTIONS)
    assert not (set(profile.standing) | set(profile.gated)) & set(FORBIDDEN_ACTIONS)


def test_reviewer_is_read_only_and_has_no_gated_actions():
    reviewer = ROLE_PROFILES["reviewer"]
    assert reviewer.read_only is True
    assert set(reviewer.gated) == set()
    assert "local_fix" not in reviewer.standing


def test_only_backend_and_qa_may_change_the_copy():
    fixers = {role for role, profile in ROLE_PROFILES.items()
              if not profile.read_only}
    assert fixers == {"backend", "qa"}


def test_ai_gpu_role_may_request_a_gpu_lease():
    assert "request_gpu_lease" in ROLE_PROFILES["ai_gpu"].standing


# -- the gate enforces exactly these profiles ------------------------------
def test_gate_allows_every_role_standing_action(tmp_path):
    gate = _gate(tmp_path)
    for role, expected in EXPECTED_ROLES.items():
        for action in expected["standing"]:
            decision = gate.decide(action, actor=role, subject="lab-copy")
            assert decision.allowed, f"{role} should stand {action}"
            assert decision.rule == "standing_permission"


def test_gate_denies_role_gated_action_until_the_owner_grants_it(tmp_path):
    gate = _gate(tmp_path)
    decision = gate.decide("create_lab_issue", actor="qa", subject="finding-1")
    assert not decision.allowed
    assert decision.rule == "no_active_grant"

    _grant(gate, "qa", "create_lab_issue", "finding-1")
    granted = gate.decide("create_lab_issue", actor="qa", subject="finding-1")
    assert granted.allowed and granted.rule == "owner_grant"
    # the grant is subject-scoped: another subject stays denied
    assert not gate.decide("create_lab_issue", actor="qa",
                           subject="finding-2").allowed


@pytest.mark.parametrize(
    "role,action",
    [("reviewer", "local_fix"), ("frontend", "run_tests"),
     ("architect", "local_fix"), ("devops", "request_gpu_lease")],
)
def test_gate_denies_actions_outside_the_role_profile(tmp_path, role, action):
    gate = _gate(tmp_path)
    decision = gate.decide(action, actor=role, subject="")
    assert not decision.allowed
    assert decision.rule == "role_permission"


@pytest.mark.parametrize("role", sorted(EXPECTED_ROLES))
@pytest.mark.parametrize("action", sorted(FORBIDDEN_ACTIONS))
def test_forbidden_actions_are_denied_for_every_role(tmp_path, role, action):
    gate = _gate(tmp_path)
    decision = gate.decide(action, actor=role, subject="")
    assert not decision.allowed
    assert decision.rule == "forbidden_action"


def test_unknown_actor_keeps_the_classic_lab_rules(tmp_path):
    gate = _gate(tmp_path)
    assert gate.decide("read_state", actor="lab").allowed
    gated = gate.decide("create_lab_issue", actor="lab", subject="s")
    assert not gated.allowed and gated.rule == "no_active_grant"
    unknown = gate.decide("run_everything", actor="lab")
    assert not unknown.allowed and unknown.rule == "unknown_action"


def test_role_decisions_are_audited(tmp_path):
    gate = _gate(tmp_path)
    gate.decide("local_fix", actor="reviewer", subject="")
    records = [r for r in gate.trail.records() if r.get("action") == "local_fix"]
    assert records and records[-1]["decision"] == "DENIED"
    assert records[-1]["rule"] == "role_permission"
    assert records[-1]["actor"] == "reviewer"
    assert gate.trail.verify()["valid"]


# -- the parser is strict and fail-closed ----------------------------------
SECTION = """### 1. worker
**STANDING_PERMISSIONS**
- `read_state`

**GATED_ACTIONS**
- `create_lab_issue`
"""


def test_parser_rejects_an_unknown_standing_action(tmp_path):
    path = _write_profiles(tmp_path, SECTION.replace(
        "- `read_state`", "- `run_everything`"))
    with pytest.raises(RoleProfilesError, match="unknown standing actions"):
        _load(path)


def test_parser_rejects_a_forbidden_action_claim(tmp_path):
    path = _write_profiles(tmp_path, SECTION.replace(
        "- `read_state`", "- `push`"))
    # `push` is unknown to standing first, so the caller's policy set has to
    # contain it for the forbidden-claim rule itself to be exercised.
    with pytest.raises(RoleProfilesError, match="forbidden actions claimed"):
        load_role_profiles(path, standing=STANDING_PERMISSIONS | {"push"},
                           gated=GATED_ACTIONS, forbidden=FORBIDDEN_ACTIONS)
    with pytest.raises(RoleProfilesError, match="unknown standing actions"):
        _load(path)


def test_parser_rejects_an_action_that_is_both_standing_and_gated(tmp_path):
    body = SECTION.replace("**STANDING_PERMISSIONS**\n- `read_state`",
                           "**STANDING_PERMISSIONS**\n- `create_lab_issue`")
    path = _write_profiles(tmp_path, body)
    with pytest.raises(RoleProfilesError, match="both standing and gated"):
        load_role_profiles(path,
                           standing=STANDING_PERMISSIONS | {"create_lab_issue"},
                           gated=GATED_ACTIONS, forbidden=FORBIDDEN_ACTIONS)


def test_parser_rejects_a_role_without_standing_permissions(tmp_path):
    path = _write_profiles(tmp_path, """### 1. worker
**GATED_ACTIONS**
- `create_lab_issue`
""")
    with pytest.raises(RoleProfilesError, match="STANDING_PERMISSIONS is empty"):
        _load(path)


def test_parser_rejects_duplicate_role_sections(tmp_path):
    path = _write_profiles(tmp_path, SECTION + SECTION.replace("### 1. worker",
                                                               "### 2. worker"))
    with pytest.raises(RoleProfilesError, match="duplicate role section"):
        _load(path)


def test_parser_reports_an_unreadable_document(tmp_path):
    with pytest.raises(RoleProfilesError, match="cannot read"):
        _load(tmp_path / "missing.md")


def test_parser_reads_the_real_document():
    profiles = _load(None)
    assert profiles == ROLE_PROFILES
