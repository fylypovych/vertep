"""Combined, configurable reports over the one laboratory backend state.

The panel, the CLI and the agents all ask this builder for the same report, so
a combined view (status, queue, issues, rights, transfers, notifications) can
never diverge between clients (Issue #251, U3; Issue #107 approved UX:
configurable combined reports).
"""

from datetime import datetime, timezone

from .governance import Governance
from .notifications import NotificationSettings
from .orchestrator import Orchestrator
from .policy import PolicyGate
from .state_store import read_jsonl

#: Every section a client may request; the panel's toggles map 1:1 to these.
SECTIONS: tuple = ("status", "queue", "roles", "gpu", "budget", "policy",
                   "issues", "evidence", "rights", "grants", "transfers",
                   "notifications", "audit")


class ReportError(ValueError):
    """The requested report configuration is not valid."""


class ReportBuilder:
    """Assemble a report from live gate/orchestrator/governance state."""

    def __init__(self, orchestrator: Orchestrator, gate: PolicyGate,
                 governance: Governance, notifications: NotificationSettings):
        self.orchestrator = orchestrator
        self.gate = gate
        self.governance = governance
        self.notifications = notifications

    @staticmethod
    def available() -> list:
        return list(SECTIONS)

    def _section(self, name: str) -> dict:
        status = self.orchestrator.status()
        if name == "status":
            return {key: status.get(key) for key in
                    ("workspace", "policy", "budget", "roles", "executor",
                     "gpu", "lease", "handoff")}
        if name == "queue":
            return {"queue": status.get("queue", [])}
        if name == "roles":
            return {"roles": status.get("roles", [])}
        if name == "gpu":
            return status.get("gpu") or {"enabled": False}
        if name == "budget":
            return self.orchestrator.budget.usage()
        if name == "policy":
            return self.gate.status()
        if name == "issues":
            return {"created": self.governance.created(),
                    "closed": self.governance.closed()}
        if name == "evidence":
            return {"evidence": self.governance.ir_evidence()}
        if name == "rights":
            return {"requests": self.gate.rights_requests()}
        if name == "grants":
            return {"grants": [grant.as_dict() for grant in self.gate.grants()]}
        if name == "transfers":
            return {"transfers": self.governance.transfers()}
        if name == "notifications":
            return self.notifications.load()
        if name == "audit":
            trail = self.gate.trail
            records = trail.records()
            return {"verification": trail.verify(),
                    "recent": records[-20:],
                    "total_records": len(records)}
        raise ReportError(f"unknown report section {name}")

    def build(self, sections=None) -> dict:
        """Build the requested sections; ``None`` or empty means all of them."""
        requested = [str(item).strip() for item in (sections or SECTIONS)
                     if str(item).strip()]
        if not requested:
            requested = list(SECTIONS)
        unknown = sorted(set(requested) - set(SECTIONS))
        if unknown:
            raise ReportError("unknown section(s): " + ", ".join(unknown))
        payload = {name: self._section(name) for name in requested}
        return {"generated_at": datetime.now(timezone.utc).isoformat(),
                "sections": requested, "report": payload}
