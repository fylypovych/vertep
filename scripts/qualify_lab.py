#!/usr/bin/env python3
"""Laboratory qualification harness for Issue #252 (``ir.0.0.2.52``).

The harness is the executable half of the R1–R6 procedure of the laboratory:
it observes the stand, collects the evidence and decides whether the matrix is
proven.  It never invents a fact — a row that cannot be observed is reported
as ``BLOCKED`` with the concrete missing prerequisite.

Modes:

- ``--plan``   — static catalog of the rows, their mandatory facts and the
  owner rights they depend on; a plan is not a proof (exit code 1);
- ``--collect`` — run the real collectors and write the evidence document
  (exit code 0 only when every row passes);
- ``--validate FILE`` — independently re-check an existing evidence document
  (exit code 0 only when it is complete and verified).

Exit codes: 0 — the matrix is proven; 1 — not proven (partial, blocked or
failed); 2 — the harness could not run at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.qualification import (CRITERIA, ISSUE, TOOL, build_document,  # noqa: E402
                               overall_status, summarize, validate_evidence)
from lab.qualification_run import QualificationContext, run_all  # noqa: E402


def _ensure_utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass


def _prerequisites(context: QualificationContext) -> list[dict]:
    """Which owner rights and environment prerequisites are still missing."""
    checks = [
        {"id": "LAB_VERTEP_URL", "purpose": "production-priority resource endpoint (R2, R3)",
         "present": bool(context.vertep_url)},
        {"id": "LAB_IDLE_SHARING_ENABLED", "purpose": "owner activation of GPU sharing (R2)",
         "present": context.idle_sharing_enabled},
        {"id": "LAB_ROLE_EXECUTOR_URL", "purpose": "real model routes (R4)",
         "present": bool(context.executor_url)},
        {"id": "LAB_ROLE_EXECUTOR_ENABLED", "purpose": "real model routes (R4)",
         "present": context.executor_enabled},
        {"id": "LAB_WORKSPACE", "purpose": "isolated Git copy on the VM (R4)",
         "present": context.workspace_root is not None},
        {"id": "LAB_NETWORK_ALLOWLIST", "purpose": "egress allowlist (R1)",
         "present": bool(context.network_allowlist)},
        {"id": "LAB_POLICY_GATE_ACTIVE", "purpose": "24/7 activation (R5)",
         "present": False},
        {"id": "evidence/r1-lifecycle.txt", "purpose": "start/stop/restart evidence (R1)",
         "present": (context.evidence_dir / "r1-lifecycle.txt").is_file()},
        {"id": "evidence/r3-hang.json", "purpose": "real inference hang (R3)",
         "present": (context.evidence_dir / "r3-hang.json").is_file()},
        {"id": "evidence/r3-restart.json", "purpose": "VM/Dispatcher restart (R3)",
         "present": (context.evidence_dir / "r3-restart.json").is_file()},
        {"id": "evidence/r3-quota.json", "purpose": "quota exhaustion (R3)",
         "present": (context.evidence_dir / "r3-quota.json").is_file()},
        {"id": "evidence/r5-schedule.json", "purpose": "24/7 schedule window (R5)",
         "present": (context.evidence_dir / "r5-schedule.json").is_file()},
        {"id": "evidence/verification.json", "purpose": "independent verification (R6)",
         "present": (context.evidence_dir / "verification.json").is_file()},
    ]
    return checks


def build_plan(context: QualificationContext) -> dict:
    """The static catalog of the matrix with its missing prerequisites."""
    return {
        "tool": TOOL,
        "issue": ISSUE,
        "mode": "plan",
        "criteria": [item.as_dict() for item in CRITERIA],
        "prerequisites": _prerequisites(context),
        "note": "A plan is not a proof: only a collected and independently verified "
                "evidence document may pass R1–R6.",
    }


def _summarise(rows: list) -> dict:
    document = build_document(rows)
    return {
        "tool": TOOL,
        "issue": ISSUE,
        "mode": "collect",
        "overall": document["overall"],
        "summary": document["summary"],
        "problems": document["problems"],
        "missing": document["missing"],
        "rows": [
            {"criterion": row.get("criterion"), "status": row.get("status"),
             "actual": row.get("actual")}
            for row in rows
        ],
    }


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Vertep laboratory qualification (Issue %s)" % ISSUE)
    parser.add_argument("--repo-root", default=str(ROOT),
                        help="Корінь репозиторію з кодом лабораторії.")
    parser.add_argument("--evidence-dir", default=None,
                        help="Каталог доказів (за замовчуванням LAB_QUALIFICATION_EVIDENCE_DIR "
                             "або каталог лабораторії поза робочим деревом).")
    parser.add_argument("--state-dir", default=None,
                        help="Каталог стану лабораторії (LAB_STATE_DIR).")
    parser.add_argument("--output", type=Path, default=None,
                        help="Файл для JSON-документа доказів.")
    parser.add_argument("--plan", action="store_true",
                        help="Показати каталог R1–R6 і відсутні передумови (не є доказом).")
    parser.add_argument("--collect", action="store_true",
                        help="Зібрати реальні докази на стенді.")
    parser.add_argument("--validate", type=Path, default=None,
                        help="Незалежно перевірити вже зібраний документ доказів.")
    parser.add_argument("--plan-only", action="store_true",
                        help="Дозволити код 0 для статичного плану (не є доказом).")
    args = parser.parse_args(argv)

    if args.validate is not None:
        try:
            document = json.loads(args.validate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print(f"cannot read evidence document: {error}", file=sys.stderr)
            return 2
        rows = document.get("rows") or []
        verdict = validate_evidence(rows)
        result = {
            "tool": TOOL,
            "issue": ISSUE,
            "mode": "validate",
            "source": str(args.validate),
            "overall": overall_status(rows),
            "summary": summarize(rows),
            "problems": verdict["problems"],
            "missing": verdict["missing"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["overall"] == "PASS" else 1

    context = QualificationContext.from_environment(
        repo_root=args.repo_root, evidence_dir=args.evidence_dir, state_dir=args.state_dir)
    context.evidence_dir.mkdir(parents=True, exist_ok=True)
    context.state_dir.mkdir(parents=True, exist_ok=True)

    if args.plan or not args.collect:
        plan = build_plan(context)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if args.plan_only:
            return 0
        return 1

    rows = run_all(context)
    document = build_document(rows, context={
        "repo_root": str(context.repo_root),
        "evidence_dir": str(context.evidence_dir),
        "state_dir": str(context.state_dir),
        "actor": context.actor,
        "prerequisites": _prerequisites(context),
    })
    encoded = json.dumps(document, ensure_ascii=False, indent=2)
    output = args.output or (context.evidence_dir / "evidence.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded + "\n", encoding="utf-8")
    print(json.dumps(_summarise(rows), ensure_ascii=False, indent=2))
    print(f"evidence document: {output}", file=sys.stderr)
    return 0 if document["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
