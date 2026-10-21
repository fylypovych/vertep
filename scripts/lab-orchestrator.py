#!/usr/bin/env python3
"""Scheduled entry point of the autonomous laboratory (Issue #107).

Intended to be driven by a systemd timer on the laboratory VM.  One invocation
performs at most one bounded run cycle: isolation check, budget/quota check,
cheap static checks, then the gated work — and it always leaves the GPU lease
released and the state on disk consistent, so a restart loses nothing.

The script never pushes, releases, creates branches/PRs or closes issues: those
surfaces are refused by ``lab.policy``.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab.budget import BudgetLedger, LabPaused, QuotaExceeded  # noqa: E402
from lab.executor import executor_from_environment  # noqa: E402
from lab.gpu_lease import FakeResourceProvider, VertepResourceClient  # noqa: E402
from lab.orchestrator import Orchestrator  # noqa: E402
from lab.policy import PolicyGate  # noqa: E402
from lab.workspace import Workspace  # noqa: E402


def build_provider(arguments):
    """Real adapter when configured, explicit fake for demos, none otherwise.

    A silent fake would let a scheduled run look green against production it
    never touched (Issue #250, L2), so the fake provider must be asked for.
    """
    base_url = arguments.vertep_url or os.getenv("LAB_VERTEP_URL", "")
    if base_url:
        return VertepResourceClient(base_url, enabled=arguments.idle_sharing)
    if arguments.fake_provider:
        return FakeResourceProvider()
    return None


def build_executor(arguments):
    """Resolve the role executor — fail-closed without an owner endpoint."""
    url = arguments.role_executor_url
    if url:
        return executor_from_environment(url, enabled=True)
    return executor_from_environment(os.getenv("LAB_ROLE_EXECUTOR_URL", ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Один цикл автономної лабораторії Vertep")
    parser.add_argument("--state-dir", default=os.getenv("LAB_STATE_DIR",
                                                         "/var/lib/vertep-lab"))
    parser.add_argument("--workspace", default=os.getenv("LAB_WORKSPACE",
                                                         "/srv/vertep-lab/repo"))
    parser.add_argument("--vertep-url", default="")
    parser.add_argument("--idle-sharing", action="store_true",
                        help="дозволити idle GPU lease (лише після реальних гарантій)")
    parser.add_argument("--fake-provider", action="store_true",
                        help="явно увімкнути fake resource provider (для демо/тестів)")
    parser.add_argument("--role-executor-url", default="",
                        help="явний endpoint role executor (інакше LAB_ROLE_EXECUTOR_URL "
                             "+ LAB_ROLE_EXECUTOR_ENABLED)")
    parser.add_argument("--max-runs", type=int, default=1)
    parser.add_argument("--report", default="")
    arguments = parser.parse_args(argv)

    state_dir = Path(arguments.state_dir)
    workspace = Workspace(Path(arguments.workspace))
    workspace.prepare()
    gate = PolicyGate(state_dir)
    budget = BudgetLedger(state_dir / "budget")
    provider = build_provider(arguments)
    executor = build_executor(arguments)
    orchestrator = Orchestrator(workspace, gate, budget, provider=provider,
                                executor=executor)

    try:
        outcomes = orchestrator.run_cycle(max_runs=arguments.max_runs)
        status = orchestrator.status()
        exit_code = 0 if all(item["state"] == "COMPLETED" for item in outcomes) else 3
    except (QuotaExceeded, LabPaused) as error:
        outcomes = [{"state": "PAUSED", "reason": str(error)}]
        status = orchestrator.status()
        exit_code = 3
    finally:
        # Never leave a lease behind, even on an unexpected failure.
        if orchestrator.leases is not None and orchestrator.leases.lease is not None:
            orchestrator.leases.release(reason="cycle end")

    report = {"outcomes": outcomes, "status": status, "audit": gate.trail.verify()}
    if arguments.report:
        target = Path(arguments.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                          encoding="utf-8")
    else:
        print(json.dumps({"states": [item["state"] for item in outcomes],
                          "audit_valid": report["audit"]["valid"]}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
