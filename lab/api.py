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

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .budget import BudgetLedger
from .governance import Governance, IssueTransport
from .gpu_lease import FakeResourceProvider, ResourceRequirement
from .orchestrator import Orchestrator
from .policy import PolicyGate
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


class LeaseRequest(BaseModel):
    model: str = "qwen2.5-coder:7b"
    vram_mb: int = 8192


def create_app(state_dir=None, workspace_root=None, *, provider=None) -> FastAPI:
    """Build the laboratory API around one shared gate instance."""
    state = Path(state_dir or os.getenv("LAB_STATE_DIR", "/var/lib/vertep-lab"))
    workspace = Workspace(Path(workspace_root or os.getenv("LAB_WORKSPACE",
                                                           "/srv/vertep-lab/repo")))
    workspace.prepare()
    gate = PolicyGate(state)
    budget = BudgetLedger(state / "budget")
    provider = provider if provider is not None else FakeResourceProvider()
    orchestrator = Orchestrator(workspace, gate, budget, provider=provider)
    governance = Governance(gate, IssueTransport(dry_run=True),
                            state_dir=state / "governance")

    app = FastAPI(title="Vertep Laboratory API", version="1")
    app.state.context = {"gate": gate, "budget": budget, "orchestrator": orchestrator,
                         "governance": governance, "workspace": workspace,
                         "leases": orchestrator.leases}

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
    def register_grant(request: GrantRequest) -> dict:
        try:
            grant = gate.register_grant(actor=request.actor, action=request.action,
                                        subject=request.subject,
                                        approved_by=request.approved_by,
                                        expires_at=request.expires_at,
                                        environment=request.environment,
                                        scope_version=request.scope_version)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"grant": grant.as_dict()}

    @app.post("/api/lab/grants/{grant_id}/revoke")
    def revoke_grant(grant_id: str) -> dict:
        return {"revoked": gate.revoke_grant(grant_id)}

    @app.post("/api/lab/tasks")
    def enqueue(request: TaskRequest) -> dict:
        try:
            task = orchestrator.enqueue(request.kind, role=request.role,
                                        payload=request.payload)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"task": task.as_dict()}

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

    @app.get("/api/lab/issues/{kind}")
    def issue_surface(kind: str, subject: str = "") -> dict:
        """Read-only probe of a refused surface, so the panel can explain why."""
        if kind == "push":
            return governance.push()
        probes = {"i": governance.create_i_issue, "ir": governance.create_ir_issue,
                  "var": governance.create_var_issue, "edit": governance.edit_issue,
                  "close": governance.close_issue}
        if kind in probes:
            return probes[kind](subject)
        raise HTTPException(404, f"unknown issue surface {kind}")

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


