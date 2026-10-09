"""Fail-closed policy gate — the single authority for every laboratory action.

``AGENTS.md`` §4.6: "Policy gate перевіряє кожну дію, веде audit trail рішень та
блокує обхід через CLI, агентів або Web.  До фактичної реалізації, тестування та
активації gate жодні нові автономні повноваження не діють."

Rules encoded here, in order of evaluation:

1. **Deny by default.**  An action that is not explicitly permitted is refused.
2. **Publication is never autonomous.**  ``push``, ``release``, branch, PR and
   any write to ``i``/``ir``/``var`` issues require a standing owner grant that
   this gate refuses to mint for itself; it can only verify one.
3. **Only new ``lab`` issues** may be created, and only after the finding is
   validated (duplicate check, evidence, reproduction, risk, expected result,
   boundaries) and the gate is explicitly activated by the owner.
4. **Temporary grants are scoped and expire.**  Every grant names the actor,
   action, subject, environment, expiry and the owner who approved it.  An
   expired, revoked, unknown or partially matching grant denies.
5. **Every decision is audited** through :class:`lab.audit.AuditTrail`, so a
   bypass attempt is visible even when it is blocked.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

from .audit import AuditTrail
from .state_store import read_json, write_json

#: Actions the laboratory may always perform inside its own isolated copy.
STANDING_PERMISSIONS = frozenset({
    "static_checks", "local_fix", "run_tests", "read_state", "write_report",
    "request_gpu_lease", "handoff",
})

#: Actions that stay forbidden regardless of configuration: publication,
#: runtime changes and any rewrite of an existing issue belong to the owner
#: (``AGENTS.md`` §0.1, §0.2, §4.6).
FORBIDDEN_ACTIONS = frozenset({
    "push", "release", "create_branch", "create_pr", "merge",
    "modify_production", "write_production_database", "write_production_volume",
    "access_production_secrets", "edit_issue", "close_issue", "comment_issue",
})

#: Actions that may only run with an explicit, non-expired owner grant.
GATED_ACTIONS = frozenset({
    "create_lab_issue", "create_issue", "create_i_issue", "create_ir_issue",
    "create_var_issue", "standalone_verification", "delegate_close_ir",
    "consolidate_ir",
})

ISSUE_TYPES = ("lab", "i", "ir", "var")
_IDENTIFIER = re.compile(r"^(lab|i|ir|var)\.\d+\.\d+\.\d+\.\d+$")


class Action(str, Enum):
    """Every operation the gate knows how to rule on."""

    STATIC_CHECKS = "static_checks"
    LOCAL_FIX = "local_fix"
    RUN_TESTS = "run_tests"
    READ_STATE = "read_state"
    WRITE_REPORT = "write_report"
    REQUEST_GPU_LEASE = "request_gpu_lease"
    HANDOFF = "handoff"
    CREATE_LAB_ISSUE = "create_lab_issue"
    CREATE_ISSUE = "create_issue"
    EDIT_ISSUE = "edit_issue"
    CLOSE_ISSUE = "close_issue"
    COMMENT_ISSUE = "comment_issue"
    CREATE_I_ISSUE = "create_i_issue"
    CREATE_IR_ISSUE = "create_ir_issue"
    CREATE_VAR_ISSUE = "create_var_issue"
    STANDALONE_VERIFICATION = "standalone_verification"
    DELEGATE_CLOSE_IR = "delegate_close_ir"
    CONSOLIDATE_IR = "consolidate_ir"
    PUSH = "push"
    RELEASE = "release"
    CREATE_BRANCH = "create_branch"
    CREATE_PR = "create_pr"
    MERGE = "merge"
    MODIFY_PRODUCTION = "modify_production"
    WRITE_PRODUCTION_DATABASE = "write_production_database"
    WRITE_PRODUCTION_VOLUME = "write_production_volume"
    ACCESS_PRODUCTION_SECRETS = "access_production_secrets"


class PolicyDenied(RuntimeError):
    """Raised by :meth:`PolicyGate.require` when an action is not allowed."""

    def __init__(self, decision: "Decision"):
        self.decision = decision
        super().__init__(f"{decision.action} denied by policy gate: {decision.reason}")


@dataclass(frozen=True)
class Decision:
    """One gate ruling; always audited."""

    allowed: bool
    action: str
    actor: str
    subject: str
    rule: str
    reason: str
    grant: "Grant | None" = None

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "action": self.action, "actor": self.actor,
                "subject": self.subject, "rule": self.rule, "reason": self.reason,
                "grant": self.grant.as_dict() if self.grant else None}


@dataclass(frozen=True)
class Grant:
    """An owner-approved, expiring permission for exactly one operation."""

    grant_id: str
    actor: str
    action: str
    subject: str
    approved_by: str
    granted_at: str
    expires_at: str
    environment: str = "lab"
    scope_version: str = ""
    revoked: bool = False

    def matches(self, actor: str, action: str, subject: str) -> bool:
        return (self.actor == actor and self.action == action
                and self.subject == subject and not self.revoked)

    def active(self, now: datetime | None = None) -> bool:
        if self.revoked:
            return False
        now = now or datetime.now(timezone.utc)
        try:
            expires = datetime.fromisoformat(self.expires_at)
        except (TypeError, ValueError):
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now <= expires

    def as_dict(self) -> dict:
        return {"grant_id": self.grant_id, "actor": self.actor, "action": self.action,
                "subject": self.subject, "approved_by": self.approved_by,
                "granted_at": self.granted_at, "expires_at": self.expires_at,
                "environment": self.environment, "scope_version": self.scope_version,
                "revoked": self.revoked}


def _action_name(action) -> str:
    return action.value if isinstance(action, Action) else str(action)


def _grant_from(item: dict) -> Grant | None:
    try:
        return Grant(**{key: item[key] for key in (
            "grant_id", "actor", "action", "subject", "approved_by",
            "granted_at", "expires_at") if key in item},
            environment=str(item.get("environment", "lab")),
            scope_version=str(item.get("scope_version", "")),
            revoked=bool(item.get("revoked", False)))
    except TypeError:
        return None


@dataclass
class PolicyGate:
    """Fail-closed authorisation and audit point for the laboratory.

    ``state_dir`` holds ``grants.json`` and ``audit.jsonl``.  ``activated`` is
    the explicit owner switch: while it is ``False`` only standing permissions
    work, so no autonomous power exists before the gate is enabled
    (``AGENTS.md`` §4.6).  ``LAB_POLICY_GATE_ACTIVE`` may override it.
    """

    state_dir: Path
    activated: bool = False
    trail: AuditTrail = field(init=False)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        override = os.getenv("LAB_POLICY_GATE_ACTIVE", "").lower()
        if override in {"true", "1", "yes"}:
            self.activated = True
        elif override in {"false", "0", "no"}:
            self.activated = False
        self.trail = AuditTrail(self.state_dir / "audit.jsonl")

    # -- grants ----------------------------------------------------------
    @property
    def _grants_path(self) -> Path:
        return self.state_dir / "grants.json"

    def grants(self) -> list[Grant]:
        payload = read_json(self._grants_path, default=[]) or []
        if not isinstance(payload, list):
            return []
        parsed = (_grant_from(item) for item in payload if isinstance(item, dict))
        return [grant for grant in parsed if grant is not None]

    def _save_grants(self, grants: Iterable[Grant]) -> None:
        write_json(self._grants_path, [grant.as_dict() for grant in grants])

    def register_grant(self, *, actor: str, action: str, subject: str, approved_by: str,
                       expires_at: str, environment: str = "lab", scope_version: str = "",
                       grant_id: str = "") -> Grant:
        """Persist an owner-approved grant.  The owner creates it, not the gate."""
        name = _action_name(action)
        if name in FORBIDDEN_ACTIONS:
            raise ValueError(f"{name} can never be granted to the laboratory")
        if name not in GATED_ACTIONS:
            raise ValueError(f"{name} is not a gated action")
        if not str(approved_by).strip():
            raise ValueError("a grant requires the approving owner identity")
        grant = Grant(grant_id=grant_id or f"g-{len(self.grants()) + 1:04d}",
                      actor=actor, action=name, subject=subject, approved_by=approved_by,
                      granted_at=datetime.now(timezone.utc).isoformat(),
                      expires_at=expires_at, environment=environment,
                      scope_version=scope_version)
        self._save_grants([*self.grants(), grant])
        self.trail.append(actor=approved_by, action=f"grant:{name}", subject=subject,
                          decision="GRANTED", rule="owner_approval",
                          reason=f"grant {grant.grant_id} until {expires_at}")
        return grant

    def revoke_grant(self, grant_id: str, actor: str = "owner") -> bool:
        """Revoke a grant; the revocation is effective for every later decision."""
        remaining: list[Grant] = []
        found = False
        for grant in self.grants():
            if grant.grant_id == grant_id and not grant.revoked:
                found = True
                data = grant.as_dict()
                data["revoked"] = True
                remaining.append(Grant(**data))
            else:
                remaining.append(grant)
        if found:
            self._save_grants(remaining)
            self.trail.append(actor=actor, action=f"revoke:{grant_id}", subject="",
                              decision="REVOKED", rule="owner_action",
                              reason="delegation cancelled")
        return found

    def active_grant(self, actor: str, action: str, subject: str) -> Grant | None:
        name = _action_name(action)
        for grant in self.grants():
            if grant.matches(actor, name, subject) and grant.active():
                return grant
        return None

    # -- decisions -------------------------------------------------------
    def decide(self, action, *, actor: str, subject: str = "",
               now: datetime | None = None) -> Decision:
        name = _action_name(action)
        now = now or datetime.now(timezone.utc)
        if name in FORBIDDEN_ACTIONS:
            return self._record(False, name, actor, subject, "forbidden_action",
                                "publication and production writes are never autonomous",
                                now=now)
        if name in STANDING_PERMISSIONS:
            return self._record(True, name, actor, subject, "standing_permission",
                                "allowed inside the isolated laboratory copy", now=now)
        if name not in GATED_ACTIONS:
            return self._record(False, name, actor, subject, "unknown_action",
                                "deny by default: no rule permits this action", now=now)
        if not self.activated:
            return self._record(False, name, actor, subject, "gate_inactive",
                                "policy gate is not activated yet", now=now)
        grant = self.active_grant(actor, name, subject)
        if grant is None:
            return self._record(False, name, actor, subject, "no_active_grant",
                                "no matching, unexpired owner grant", now=now)
        return self._record(True, name, actor, subject, "owner_grant",
                            f"grant {grant.grant_id}", grant=grant, now=now)

    def allow(self, action, *, actor: str, subject: str = "") -> bool:
        return self.decide(action, actor=actor, subject=subject).allowed

    def require(self, action, *, actor: str, subject: str = "") -> Decision:
        decision = self.decide(action, actor=actor, subject=subject)
        if not decision.allowed:
            raise PolicyDenied(decision)
        return decision

    def _record(self, allowed: bool, action: str, actor: str, subject: str, rule: str,
                reason: str, grant: Grant | None = None,
                now: datetime | None = None) -> Decision:
        decision = Decision(allowed=allowed, action=action, actor=actor, subject=subject,
                            rule=rule, reason=reason, grant=grant)
        self.trail.append(actor=actor or "unknown", action=action, subject=subject,
                          decision="ALLOWED" if allowed else "DENIED", rule=rule,
                          reason=reason,
                          extra={"grant_id": grant.grant_id} if grant else None)
        return decision

    # -- reporting -------------------------------------------------------
    def status(self) -> dict:
        return {"activated": self.activated,
                "active_grants": [grant.as_dict() for grant in self.grants()
                                  if grant.active()],
                "standing_permissions": sorted(STANDING_PERMISSIONS),
                "forbidden_actions": sorted(FORBIDDEN_ACTIONS),
                "audit": self.trail.verify()}


def validate_lab_finding(finding: dict) -> dict:
    """Validate an automatic ``lab`` finding against ``AGENTS.md`` §4.6.

    A ``lab`` issue may only describe a *confirmed* defect, regression, security
    violation or non-conformance with an already approved requirement.  New
    product ideas are rejected here, before anything reaches GitHub.
    """
    problems: list[str] = []
    if not isinstance(finding, dict):
        return {"valid": False, "problems": ["finding must be an object"]}
    for key in ("title", "evidence", "reproduction", "risk", "expected_result",
                "boundaries", "source", "approved_scope"):
        value = finding.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"missing {key}")
    identifier = str(finding.get("identifier", ""))
    if identifier and not _IDENTIFIER.match(identifier):
        problems.append("identifier must look like lab.A.B.C.D")
    kind = str(finding.get("kind", "defect"))
    if kind not in {"defect", "regression", "security", "nonconformance"}:
        problems.append("kind must be defect, regression, security or nonconformance")
    title = str(finding.get("title", "")).lower()
    if any(marker in title for marker in ("ідея", "idea", "feature request",
                                          "пропозиція функції")):
        problems.append("lab is not a channel for product ideas")
    return {"valid": not problems, "problems": problems}

