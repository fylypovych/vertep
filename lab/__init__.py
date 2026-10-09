"""Autonomous development laboratory for Vertep (Issue #107 / var.0.0.1.7).

The laboratory is a separate, isolated subsystem that lives next to — never
inside — production Vertep.  It performs scheduled code audits, local fixes in
its own working copy, cheap static checks before any AI call and reports the
result to the owner.  Nothing here may publish code: every privileged action
goes through :mod:`lab.policy`, which is fail-closed.

Design rules taken from the issue and ``AGENTS.md`` §4.6/§4.7:

* no autonomous push, release, branch, PR or issue write;
* only new ``lab`` issues may be created, and only through the policy gate;
* GPU is used solely under a lease granted by the production dispatcher
  (``lab.gpu_lease``); there is no competing scheduler here;
* the laboratory never imports production CORE internals and never touches the
  production database, queue, volumes, secrets or Docker socket.

The policy gate is the single authority.  Until it is implemented, tested and
activated, no autonomous permission exists — this package ships the gate plus
its proofs.
"""

from .audit import AuditTrail
from .budget import BudgetLedger, LabPaused, PaidModelDenied, QuotaExceeded
from .gpu_lease import (FakeResourceProvider, Lease, LeaseDenied, LeaseLost,
                        LeaseManager, ResourceRequirement, ResourceUnavailable,
                        VertepResourceClient)
from .policy import (Action, Decision, Grant, PolicyDenied, PolicyGate,
                     STANDING_PERMISSIONS)
from .workspace import IsolationViolation, Workspace

__all__ = [
    "Action", "AuditTrail", "BudgetLedger", "Decision", "FakeResourceProvider",
    "Grant", "IsolationViolation", "LabPaused", "Lease", "LeaseDenied",
    "LeaseLost", "LeaseManager", "PaidModelDenied", "PolicyDenied", "PolicyGate",
    "QuotaExceeded", "ResourceRequirement", "ResourceUnavailable",
    "STANDING_PERMISSIONS", "VertepResourceClient", "Workspace",
]
