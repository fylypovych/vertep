"""GitHub governance for the autonomous laboratory (Issue #107, §"GitHub governance").

The laboratory may create **only new ``lab`` issues**, and only through this
module, which sits behind the policy gate.  Concretely:

* a finding is validated first (confirmed defect/regression/security/non-conformance
  with evidence, reproduction, risk, expected result, boundaries and a link to an
  already approved requirement) — ``lab`` is not a channel for product ideas;
* duplicates are refused before anything is published;
* ``i``/``ir``/``var`` writes, edits of foreign issues, closing, pushing, branches
  and PRs are refused by the gate;
* every attempt — allowed or denied — is audited, and a retry never duplicates an
  issue because the decision key is persisted next to the created issue.

The GitHub call itself is injected (:class:`IssueTransport`), so tests prove the
policy without a token and the production transport stays a thin ``gh`` wrapper.
"""

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .policy import Action, PolicyDenied, PolicyGate, validate_lab_finding
from .state_store import append_jsonl, read_json, write_json


class GovernanceError(RuntimeError):
    """A governance rule refused the operation."""


@dataclass
class IssueTransport:
    """Minimal GitHub surface the laboratory is allowed to use."""

    dry_run: bool = True

    def create_issue(self, title: str, body: str, labels=()) -> dict:
        raise NotImplementedError

    def find_duplicates(self, title: str) -> list:
        raise NotImplementedError


class GhIssueTransport(IssueTransport):
    """Production transport: ``gh`` with a minimal-permission token."""

    def __init__(self, repository: str, *, token: str | None = None, dry_run: bool = True):
        super().__init__(dry_run=dry_run)
        self.repository = repository
        self.token = token if token is not None else os.getenv("LAB_GITHUB_TOKEN", "")

    def _gh(self, *arguments: str) -> str:
        environment = dict(os.environ)
        if self.token:
            environment["GH_TOKEN"] = self.token
        completed = subprocess.run(["gh", *arguments], capture_output=True, text=True,
                                   env=environment, timeout=60)
        if completed.returncode != 0:
            raise GovernanceError(completed.stderr.strip()[:500] or "gh failed")
        return completed.stdout

    def create_issue(self, title: str, body: str, labels=()) -> dict:
        if self.dry_run:
            return {"number": None, "url": "", "dry_run": True, "title": title}
        command = ["issue", "create", "--repo", self.repository, "--title", title,
                   "--body", body]
        for label in labels:
            command.extend(["--label", str(label)])
        url = self._gh(*command).strip()
        return {"number": None, "url": url, "dry_run": False, "title": title}

    def find_duplicates(self, title: str) -> list:
        if self.dry_run:
            return []
        output = self._gh("issue", "list", "--repo", self.repository, "--state", "all",
                          "--search", title, "--json", "number,title", "--limit", "20")
        import json
        try:
            return json.loads(output or "[]")
        except ValueError:
            return []


class Governance:
    """The only channel through which the laboratory touches GitHub issues."""

    def __init__(self, gate: PolicyGate, transport: IssueTransport, *, state_dir,
                 actor: str = "lab", repository: str = ""):
        self.gate = gate
        self.transport = transport
        self.actor = actor
        self.repository = repository
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _created_path(self) -> Path:
        return self.state_dir / "created-issues.json"

    def created(self) -> list:
        payload = read_json(self._created_path, default=[]) or []
        return payload if isinstance(payload, list) else []

    def _already_created(self, key: str) -> dict | None:
        for record in self.created():
            if record.get("key") == key:
                return record
        return None

    # -- findings --------------------------------------------------------
    def propose_lab_issue(self, finding: dict, *, key: str = "") -> dict:
        """Validate and publish a ``lab`` finding; every other write is refused."""
        validation = validate_lab_finding(finding)
        decision_key = key or str(finding.get("identifier") or "")
        decision_key = decision_key or str(finding.get("title", ""))[:80]

        existing = self._already_created(decision_key)
        if existing is not None:
            # A retry or restart must never duplicate an issue.
            self.gate.trail.append(actor=self.actor, action="create_lab_issue",
                                   subject=decision_key, decision="DUPLICATE",
                                   rule="idempotent_retry",
                                   reason=str(existing.get("issue", "")))
            return {"created": False, "reason": "already created",
                    "issue": existing.get("issue", existing), "record": existing}

        if not validation["valid"]:
            self.gate.trail.append(actor=self.actor, action="create_lab_issue",
                                   subject=decision_key, decision="REJECTED",
                                   rule="finding_validation",
                                   reason="; ".join(validation["problems"]))
            raise GovernanceError("finding is not a confirmed defect: "
                                  + "; ".join(validation["problems"]))

        try:
            self.gate.require(Action.CREATE_LAB_ISSUE, actor=self.actor,
                              subject=decision_key)
        except PolicyDenied as error:
            return {"created": False, "reason": str(error),
                    "decision": error.decision.as_dict()}

        duplicates = self.transport.find_duplicates(str(finding.get("title", "")))
        if duplicates:
            self.gate.trail.append(actor=self.actor, action="create_lab_issue",
                                   subject=decision_key, decision="REJECTED",
                                   rule="duplicate_check",
                                   reason=f"{len(duplicates)} similar issue(s)")
            return {"created": False, "reason": "duplicate finding",
                    "duplicates": duplicates}

        issue = self.transport.create_issue(str(finding.get("title", "")),
                                            render_lab_body(finding), labels=("lab",))
        record = {"key": decision_key,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "actor": self.actor, "finding": finding, "issue": issue}
        write_json(self._created_path, [*self.created(), record])
        self.gate.trail.append(actor=self.actor, action="create_lab_issue",
                               subject=decision_key, decision="CREATED",
                               rule="owner_grant",
                               reason=str(issue.get("url") or "dry-run"))
        append_jsonl(self.state_dir / "governance.jsonl",
                     {"at": record["created_at"], "action": "create_lab_issue",
                      "subject": decision_key, "issue": issue})
        return {"created": True, "issue": issue, "record": record}

    # -- refused surfaces ------------------------------------------------
    def _refuse_or_report(self, action: Action, subject: str) -> dict:
        decision = self.gate.decide(action, actor=self.actor, subject=subject)
        return {"allowed": decision.allowed, "reason": decision.reason,
                "rule": decision.rule}

    def edit_issue(self, issue: str, body: str = "") -> dict:
        """Editing any issue — including a ``lab`` one — is never autonomous."""
        return self._refuse_or_report(Action.EDIT_ISSUE, issue)

    def close_issue(self, issue: str) -> dict:
        return self._refuse_or_report(Action.CLOSE_ISSUE, issue)

    def create_i_issue(self, title: str) -> dict:
        return self._refuse_or_report(Action.CREATE_I_ISSUE, title)

    def create_ir_issue(self, title: str) -> dict:
        return self._refuse_or_report(Action.CREATE_IR_ISSUE, title)

    def create_var_issue(self, title: str) -> dict:
        return self._refuse_or_report(Action.CREATE_VAR_ISSUE, title)

    def push(self) -> dict:
        return self._refuse_or_report(Action.PUSH, "")


def render_lab_body(finding: dict) -> str:
    """Render a ``lab`` issue body from a validated finding."""
    lines = [
        "## Тип",
        "",
        f"`lab` — знахідка автономної лабораторії ({finding.get('kind', 'defect')}).",
        "",
        "## Доказ",
        "",
        str(finding.get("evidence", "")).strip(),
        "",
        "## Відтворення",
        "",
        str(finding.get("reproduction", "")).strip(),
        "",
        "## Ризик",
        "",
        str(finding.get("risk", "")).strip(),
        "",
        "## Очікуваний результат",
        "",
        str(finding.get("expected_result", "")).strip(),
        "",
        "## Межі",
        "",
        str(finding.get("boundaries", "")).strip(),
        "",
        "## Джерело та погоджений scope",
        "",
        f"- Джерело: {str(finding.get('source', '')).strip()}",
        f"- Погоджена вимога: {str(finding.get('approved_scope', '')).strip()}",
        "",
        "Це не продуктова ідея та не нова функція: лабораторія фіксує лише",
        "підтверджений дефект, регресію, порушення безпеки або невідповідність уже",
        "затвердженим вимогам (`AGENTS.md` §4.6).",
    ]
    return "\n".join(lines)



