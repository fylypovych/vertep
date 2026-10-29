"""Laboratory backend API — one authorisation model for CLI, agents and Web.

The Angular panel of the laboratory talks to these endpoints and to nothing
else.  Every mutating endpoint goes through the very same
:class:`lab.policy.PolicyGate` instance the CLI uses, so there is no permission
logic in the frontend and no bypass through a different client.  The panel is
explicitly *not* a liveness condition: if it is down, the background
orchestrator and the policy gate keep working.
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from .budget import BudgetLedger
from .governance import Governance, GovernanceError, default_transport
from .gpu_lease import ResourceRequirement
from .notifications import NotificationSettings, SettingsError
from .orchestrator import Orchestrator
from .policy import Action, PolicyDenied, PolicyGate
from .reports import ReportBuilder, ReportError
from .workspace import Workspace


class GrantRequest(BaseModel):
    actor: str = "lab"
    action: str
    subject: str = ""
    approved_by: str
    expires_at: str
    environment: str = "lab"
    scope_version: str = ""


class CheckRequest(BaseModel):
    actor: str = "lab"
    action: str
    subject: str = ""


class TaskRequest(BaseModel):
    kind: str
    role: str = "reviewer"
    payload: dict = {}


class FindingRequest(BaseModel):
    finding: dict
    key: str = ""


class IssueCreateRequest(BaseModel):
    title: str
    identifier: str = ""
    metadata: dict = {}
    key: str = ""


class LeaseRequest(BaseModel):
    model: str = "qwen2.5-coder:7b"
    vram_mb: int = 8192
    actor: str = "lab"
    subject: str = ""


class NotificationSettingsRequest(BaseModel):
    settings: dict
    reason: str = ""


class IrEvidenceRequest(BaseModel):
    identifier: str
    evidence: dict = {}


class IrCloseRequest(BaseModel):
    identifier: str
    evidence: dict = {}


class IrConsolidateRequest(BaseModel):
    source: dict = {}
    successors: list = []
    reason: str = ""


def _require_owner(raw_request: Request) -> str:
    """Authenticate an owner act; ``approved_by``/identity never comes from a body."""
    owner_token = os.getenv("LAB_OWNER_TOKEN", "")
    supplied = raw_request.headers.get("X-Lab-Owner-Token", "")
    if not owner_token or supplied != owner_token:
        raise HTTPException(401, "owner authentication required")
    return os.getenv("LAB_OWNER_IDENTITY", "owner")


def create_app(state_dir=None, workspace_root=None, *, provider=None,
                executor=None) -> FastAPI:
    """Build the laboratory API around one shared gate instance."""
    from .executor import executor_from_environment
    state = Path(state_dir or os.getenv("LAB_STATE_DIR", "/var/lib/vertep-lab"))
    workspace = Workspace(Path(workspace_root or os.getenv("LAB_WORKSPACE",
                                                           "/srv/vertep-lab/repo")))
    workspace.prepare()
    gate = PolicyGate(state)
    budget = BudgetLedger(state / "budget")
    if executor is None:
        executor = executor_from_environment(os.getenv("LAB_ROLE_EXECUTOR_URL", ""))
    if provider is None and os.getenv("LAB_RESOURCE_URL"):
        from .gpu_lease import VertepResourceClient
        provider = VertepResourceClient(os.environ["LAB_RESOURCE_URL"],
            token=os.getenv("LAB_RESOURCE_TOKEN", ""),
            enabled=os.getenv("LAB_IDLE_SHARING_ENABLED", "false").lower() in {"true", "1", "yes"})
    orchestrator = Orchestrator(workspace, gate, budget, provider=provider,
                                executor=executor)
    governance = Governance(gate, default_transport(),
                            state_dir=state / "governance")
    notifications = NotificationSettings(gate, state_dir=state)
    reports = ReportBuilder(orchestrator, gate, governance, notifications)

    app = FastAPI(title="Vertep Laboratory API", version="1")
    app.state.context = {"gate": gate, "budget": budget, "orchestrator": orchestrator,
                         "governance": governance, "workspace": workspace,
                         "leases": orchestrator.leases,
                         "notifications": notifications, "reports": reports}

    @app.get("/api/lab/status")
    def status() -> dict:
        return orchestrator.status()

    @app.get("/api/lab/audit")
    def audit() -> dict:
        return gate.trail.verify()

    @app.get("/api/lab/audit/records")
    def audit_records(limit: int = 100) -> dict:
        records = gate.trail.records()
        return {"records": records[-max(1, min(limit, 1000)):]}

    @app.post("/api/lab/check")
    def check(request: CheckRequest) -> dict:
        return gate.decide(request.action, actor=request.actor,
                           subject=request.subject).as_dict()

    @app.post("/api/lab/grants")
    def register_grant(request: GrantRequest, raw_request: Request) -> dict:
        # Grants are an owner act: the caller must prove ownership through a
        # shared secret, and approved_by is taken from that authenticated
        # identity rather than from the request body (Issue i.0.0.2.50, L4).
        owner_identity = _require_owner(raw_request)
        try:
            grant = gate.register_grant(actor=request.actor, action=request.action,
                                         subject=request.subject,
                                         approved_by=owner_identity,
                                         expires_at=request.expires_at,
                                         environment=request.environment,
                                         scope_version=request.scope_version)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"grant": grant.as_dict()}

    @app.post("/api/lab/grants/{grant_id}/revoke")
    def revoke_grant(grant_id: str, raw_request: Request) -> dict:
        # Revocation is symmetric to minting: the same owner proof, and the
        # authenticated identity (not a body field) is audited as the actor.
        owner_identity = _require_owner(raw_request)
        return {"revoked": gate.revoke_grant(grant_id, actor=owner_identity)}

    @app.post("/api/lab/tasks")
    def enqueue(request: TaskRequest) -> dict:
        try:
            task = orchestrator.enqueue(request.kind, role=request.role,
                                        payload=request.payload)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"task": task.as_dict(), "approval_scope": task.approval_scope}

    @app.post("/api/lab/run")
    def run() -> dict:
        return orchestrator.run_once()

    @app.get("/api/lab/gpu")
    def gpu_status() -> dict:
        return orchestrator.leases.status() if orchestrator.leases else {"enabled": False}

    @app.post("/api/lab/gpu/request")
    def gpu_request(request: LeaseRequest) -> dict:
        if orchestrator.leases is None:
            raise HTTPException(409, "no resource provider configured")
        # Authorisation is checked before the effect: role standing, grant
        # subject, environment and expiry all decide first (Issue i.0.0.2.50, L4).
        try:
            gate.require(Action.REQUEST_GPU_LEASE, actor=request.actor,
                         subject=request.subject)
        except PolicyDenied as error:
            raise HTTPException(403, error.decision.reason) from error
        try:
            lease = orchestrator.leases.acquire(
                ResourceRequirement(model=request.model, vram_mb=request.vram_mb))
        except Exception as error:
            raise HTTPException(409, str(error)) from error
        return {"lease": lease.as_dict()}

    @app.post("/api/lab/gpu/release")
    def gpu_release() -> dict:
        if orchestrator.leases is None:
            raise HTTPException(409, "no resource provider configured")
        return orchestrator.leases.release(reason="api release")

    @app.post("/api/lab/issues/lab")
    def propose_lab_issue(request: FindingRequest) -> dict:
        try:
            return governance.propose_lab_issue(request.finding, key=request.key)
        except Exception as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/lab/issues/{kind}")
    def create_issue(kind: str, request: IssueCreateRequest) -> dict:
        """Create an approved ``i``/``ir``/``var`` issue through the one gate.

        The decision is the gate's: without a matching owner grant nothing
        reaches the transport, and the same numeric series is reserved as in
        the CLI (Issue #250, L5).
        """
        if kind not in ("i", "ir", "var"):
            raise HTTPException(404, f"unknown issue kind {kind}")
        try:
            return governance.create_issue(kind, request.title,
                                           identifier=request.identifier,
                                           metadata=request.metadata,
                                           key=request.key)
        except GovernanceError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/lab/issues/{kind}")
    def issue_surface(kind: str, subject: str = "") -> dict:
        """Read-only probe of a refused surface, so the panel can explain why.

        The probe decides through the gate but never touches the transport, so
        even an approved subject cannot create or close anything with GET.
        """
        try:
            return governance.probe_surface(kind, subject)
        except GovernanceError as error:
            raise HTTPException(404, str(error)) from error

    # -- panel reads (same state the CLI reads; Issue #251, U1/U2) ----------
    @app.get("/api/lab/issues")
    def issues_list() -> dict:
        return {"created": governance.created(), "closed": governance.closed()}

    @app.get("/api/lab/evidence")
    def evidence_list() -> dict:
        return {"evidence": governance.ir_evidence()}

    @app.get("/api/lab/grants")
    def grants_list() -> dict:
        return {"grants": [grant.as_dict() for grant in gate.grants()]}

    @app.get("/api/lab/rights-requests")
    def rights_requests() -> dict:
        return {"requests": gate.rights_requests()}

    @app.get("/api/lab/transfers")
    def transfers_list() -> dict:
        return {"transfers": governance.transfers()}

    @app.get("/api/lab/notifications")
    def notifications_get() -> dict:
        return notifications.load()

    @app.put("/api/lab/notifications")
    def notifications_update(request: NotificationSettingsRequest,
                             raw_request: Request) -> dict:
        """One importance store for Web and Telegram; an owner act (Issue #251, U3)."""
        owner_identity = _require_owner(raw_request)
        try:
            return notifications.update(request.settings, actor=owner_identity,
                                        reason=request.reason)
        except SettingsError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/lab/reports")
    def reports_get(sections: str = "") -> dict:
        """Combined configurable report; the client picks the sections."""
        requested = [item for item in sections.split(",") if item.strip()] \
            if sections else None
        try:
            return reports.build(requested)
        except ReportError as error:
            raise HTTPException(422, str(error)) from error

    # -- approvals centre: evidence, delegated close, consolidation --------
    @app.post("/api/lab/ir/evidence")
    def submit_evidence(request: IrEvidenceRequest) -> dict:
        try:
            return governance.submit_ir_evidence(request.identifier,
                                                 request.evidence)
        except GovernanceError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/lab/ir/close")
    def close_ir(request: IrCloseRequest, raw_request: Request) -> dict:
        """Owner-delegated close: the same proof and gate as the CLI path."""
        _require_owner(raw_request)
        return governance.delegate_close_ir(request.identifier, request.evidence)

    @app.post("/api/lab/ir/consolidate")
    def consolidate_ir(request: IrConsolidateRequest, raw_request: Request) -> dict:
        _require_owner(raw_request)
        try:
            return governance.consolidate_ir(request.source, request.successors,
                                             reason=request.reason)
        except GovernanceError as error:
            raise HTTPException(422, str(error)) from error

    return app


_app = None


def __getattr__(name: str):
    """Lazily build the default app so importing this module stays side-effect free."""
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


