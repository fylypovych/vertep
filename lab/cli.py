"""Command-line surface of the laboratory.

The CLI is a thin client of the same policy gate, state and audit trail the Web
panel and the agents use — there is no permission logic in the interface, so
bypassing the gate through the CLI is impossible by construction
(``AGENTS.md`` §4.6).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .budget import BudgetLedger
from .governance import Governance, IssueTransport
from .gpu_lease import FakeResourceProvider, LeaseManager, ResourceRequirement
from .orchestrator import Orchestrator
from .policy import Action, PolicyDenied, PolicyGate
from .workspace import Workspace


def default_state_dir() -> Path:
    return Path(os.getenv("LAB_STATE_DIR", "/var/lib/vertep-lab"))


def default_workspace() -> Workspace:
    return Workspace(Path(os.getenv("LAB_WORKSPACE", "/srv/vertep-lab/repo")),
                     repository_url=os.getenv("LAB_REPOSITORY_URL", ""),
                     branch=os.getenv("LAB_BRANCH", "main"))


def build_context(args) -> dict:
    """Wire workspace, gate, budget and (optionally) GPU lease + governance."""
    state_dir = Path(getattr(args, "state_dir", None) or default_state_dir())
    workspace = Workspace(getattr(args, "workspace", None) or
                          Path(os.getenv("LAB_WORKSPACE", "/srv/vertep-lab/repo")))
    workspace.prepare()
    gate = PolicyGate(state_dir)
    budget = BudgetLedger(state_dir / "budget")
    provider = None
    if getattr(args, "gpu", False):
        provider = FakeResourceProvider()
    orchestrator = Orchestrator(workspace, gate, budget, provider=provider)
    governance = Governance(gate, IssueTransport(dry_run=True), state_dir=state_dir / "governance")
    return {"state_dir": state_dir, "workspace": workspace, "gate": gate,
            "budget": budget, "orchestrator": orchestrator, "governance": governance,
            "leases": orchestrator.leases}


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def command_status(context) -> int:
    _print(context["orchestrator"].status())
    return 0


def command_audit(context) -> int:
    report = context["gate"].trail.verify()
    _print(report)
    return 0 if report["valid"] else 1


def command_check(context) -> int:
    """Dry-run the gate for a privileged action without executing it."""
    gate = context["gate"]
    action = context["args"].action
    subject = context["args"].subject or ""
    decision = gate.decide(action, actor=context["args"].actor, subject=subject)
    _print(decision.as_dict())
    return 0 if decision.allowed else 1


def command_grant(context) -> int:
    """Owner-only: register a scoped, expiring grant."""
    args = context["args"]
    expires = args.expires_at or (datetime.now(timezone.utc)
                                  + timedelta(hours=args.hours)).isoformat()
    try:
        grant = context["gate"].register_grant(actor=args.actor, action=args.action,
                                              subject=args.subject or "",
                                              approved_by=args.approved_by,
                                              expires_at=expires,
                                              environment=args.environment,
                                              scope_version=args.scope_version or "")
    except ValueError as error:
        _print({"granted": False, "reason": str(error)})
        return 1
    _print({"granted": True, "grant": grant.as_dict()})
    return 0


def command_revoke(context) -> int:
    revoked = context["gate"].revoke_grant(context["args"].grant_id)
    _print({"revoked": revoked})
    return 0 if revoked else 1


def command_run(context) -> int:
    outcome = context["orchestrator"].run_once()
    _print(outcome)
    return 0 if outcome.get("state") == "COMPLETED" else 2


def command_enqueue(context) -> int:
    task = context["orchestrator"].enqueue(context["args"].kind,
                                           role=context["args"].role,
                                           payload={"detail": context["args"].detail or ""})
    _print(task.as_dict())
    return 0


def command_gpu(context) -> int:
    """GPU lease lifecycle: status, request, heartbeat, release."""
    leases: LeaseManager | None = context["leases"]
    args = context["args"]
    if leases is None:
        _print({"error": "no resource provider configured; pass --gpu for the fake"})
        return 1
    if args.gpu_command == "status":
        _print(leases.status())
        return 0
    if args.gpu_command == "request":
        try:
            lease = leases.acquire(ResourceRequirement(model=args.model,
                                                       vram_mb=args.vram_mb))
        except Exception as error:
            _print({"granted": False, "reason": str(error)})
            return 1
        _print({"granted": True, "lease": lease.as_dict()})
        return 0
    if args.gpu_command == "heartbeat":
        try:
            leases.heartbeat()
        except Exception as error:
            _print({"active": False, "reason": str(error)})
            return 1
        _print({"active": True, "lease": leases.lease.as_dict() if leases.lease else None})
        return 0
    if args.gpu_command == "release":
        _print(leases.release(reason="cli release"))
        return 0
    _print({"error": f"unknown gpu command {args.gpu_command}"})
    return 1


def command_lab_issue(context) -> int:
    args = context["args"]
    finding = json.loads(Path(args.finding).read_text(encoding="utf-8")) \
        if args.finding else json.loads(sys.stdin.read() or "{}")
    try:
        outcome = context["governance"].propose_lab_issue(finding, key=args.key or "")
    except Exception as error:
        _print({"created": False, "reason": str(error)})
        return 1
    _print(outcome)
    return 0 if outcome.get("created") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vertep-lab",
        description="Керування автономною лабораторією розробки Vertep (Issue #107).")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--actor", default=os.getenv("LAB_ACTOR", "lab"))
    parser.add_argument("--gpu", action="store_true",
                        help="увімкнути керування lease (fake provider у цьому CLI)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(handler=command_status)
    sub.add_parser("audit").set_defaults(handler=command_audit)
    sub.add_parser("run").set_defaults(handler=command_run)

    check = sub.add_parser("check")
    check.add_argument("action")
    check.add_argument("--subject", default="")
    check.set_defaults(handler=command_check)

    grant = sub.add_parser("grant")
    grant.add_argument("action")
    grant.add_argument("--subject", default="")
    grant.add_argument("--approved-by", default=os.getenv("LAB_OWNER", ""))
    grant.add_argument("--hours", type=int, default=8)
    grant.add_argument("--expires-at", default="")
    grant.add_argument("--environment", default="lab")
    grant.add_argument("--scope-version", default="")
    grant.set_defaults(handler=command_grant)

    revoke = sub.add_parser("revoke")
    revoke.add_argument("grant_id")
    revoke.set_defaults(handler=command_revoke)

    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("kind")
    enqueue.add_argument("--role", default="reviewer")
    enqueue.add_argument("--detail", default="")
    enqueue.set_defaults(handler=command_enqueue)

    gpu = sub.add_parser("gpu")
    gpu.add_argument("gpu_command", choices=["status", "request", "heartbeat", "release"])
    gpu.add_argument("--model", default="qwen2.5-coder:7b")
    gpu.add_argument("--vram-mb", type=int, default=8192)
    gpu.set_defaults(handler=command_gpu)

    issue = sub.add_parser("lab-issue")
    issue.add_argument("--finding", default="", help="шлях до JSON або stdin")
    issue.add_argument("--key", default="")
    issue.set_defaults(handler=command_lab_issue)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    context = build_context(args)
    context["args"] = args
    try:
        return int(args.handler(context))
    except PolicyDenied as error:
        _print({"allowed": False, "reason": str(error), "decision": error.decision.as_dict()})
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

