#!/usr/bin/env python3
"""Issue #122 P12 — перевірна простежка retirement (§8 «Retirement»).

§8 вимагає для кожного можливого old-пункта ланцюг

    початковий критерій → заміна/новий власник → parity evidence → рішення власника

і прямо забороняє списувати інші Issues автоматично. Поки що §8 фіксує «0 безумовних
кандидатів», але це твердження, а не перевірка: будь-яка зміна таблиці перекриття може
непомітно додати кандидата без власника або доказу.

Цей скрипт читає **канонічний** текст Issue #122 (через ``gh`` або з файлу), розбирає
таблицю перекриття й видає простежену звітку: що лишається, що вилучається і що
потребує рішення власника. Він нічого не змінює в GitHub і не приймає рішення за
власника — лише показує, чи ланцюг для кожного рядка повний.

    python scripts/issue-122-retirement-trace.py
    python scripts/issue-122-retirement-trace.py --body issue-122.md --print
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ISSUE = "fylypovych/vertep#122"
_REGISTRY_HEADING = "## Повний реєстр i / ir / var"
#: Слова, які означають, що рядок таблиці пропонує вилучення пункту.
_RETIREMENT = re.compile(r"\b(retire|retired|вилучити|вилучення|списати)\b", re.I)
#: Заперечення, які знімають пропозицію вилучення: «не вилучати», «лише за рішенням».
_NEGATED = (
    "не вилуч", "не спис", "без вилуч", "без спис",
    "лише за рішенням", "тільки за рішенням", "не retire", "no retirement",
)


def proposes_retirement(text: str) -> bool:
    """True only for a real retirement proposal, not for a negated mention."""
    if not _RETIREMENT.search(text or ""):
        return False
    lowered = text.lower()
    return not any(marker in lowered for marker in _NEGATED)
#: Ланцюг, без якого вилучення не доведене: власник і parity evidence.
_OWNER = re.compile(r"#\d+")
_EVIDENCE = re.compile(r"§|P10|P11|test_|tests/")


def issue_body(issue: int = 122) -> str:
    """Read the canonical Issue body through the authenticated CLI.

    ``gh`` emits UTF-8 regardless of the console code page, so the bytes are decoded
    explicitly: on a Windows host the locale codec would otherwise mangle the body and
    the registry headings would not match.
    """
    completed = subprocess.run(
        ["gh", "issue", "view", str(issue), "--json", "body", "-q", ".body"],
        capture_output=True, timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"gh could not read the issue body: {completed.stderr.decode('utf-8', 'replace').strip()}")
    return completed.stdout.decode("utf-8")


def parse_registry(body: str) -> list[dict[str, Any]]:
    """Rows of the overlap registry, as the issue states them."""
    lines = body.splitlines()
    try:
        start = next(index for index, line in enumerate(lines)
                     if line.strip() == _REGISTRY_HEADING)
    except StopIteration:
        raise ValueError(f"the issue has no '{_REGISTRY_HEADING}' section") from None

    rows: list[dict[str, Any]] = []
    header: list[str] | None = None
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if header:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        record = dict(zip(header, cells))
        number = re.sub(r"\D", "", record.get("GitHub Issue", "") or "")
        rows.append({
            "issue": f"#{number}" if number else "",
            "identifier": record.get("Чинний ідентифікатор", ""),
            "status": record.get("Status", ""),
            "overlap": record.get("Перекриття", ""),
            "decision": record.get("Рішення / залишок", ""),
        })
    if not rows:
        raise ValueError("the overlap registry table is empty")
    return rows


def classify(row: dict[str, Any]) -> dict[str, Any]:
    """Judge one registry row against the retirement chain of §8."""
    decision = row["decision"]
    problems: list[str] = []
    if not decision:
        problems.append("no decision recorded")
    retires = proposes_retirement(decision)
    if retires:
        if not _OWNER.search(decision):
            problems.append("retirement names no replacement or owning issue")
        if not _EVIDENCE.search(decision):
            problems.append("retirement names no parity evidence")
    return {
        **row,
        "retires": retires,
        "verdict": "blocked" if problems else ("retire_with_evidence" if retires else "kept"),
        "problems": problems,
    }


def build_trace(body: str) -> dict[str, Any]:
    rows = [classify(row) for row in parse_registry(body)]
    duplicates = sorted({row["issue"] for row in rows
                         if row["issue"] and
                         sum(1 for other in rows if other["issue"] == row["issue"]) > 1})
    return {
        "issue": ISSUE,
        "rows": rows,
        "summary": {
            "rows": len(rows),
            "kept": sum(1 for row in rows if row["verdict"] == "kept"),
            "retire_with_evidence": sum(1 for row in rows
                                        if row["verdict"] == "retire_with_evidence"),
            "blocked": sum(1 for row in rows if row["verdict"] == "blocked"),
            "duplicate_issues": duplicates,
        },
        "owner_decision": (
            "required: an explicit exact old-list or the decision «порожній / не робимо old». "
            "The absence of candidates is not an approval (§4.1 P12)."
        ),
    }


def render_markdown(trace: dict[str, Any]) -> str:
    summary = trace["summary"]
    lines = [
        "# Vertep #122 P12 — retirement trace (§8)",
        "",
        f"Rows: {summary['rows']} · kept: {summary['kept']} · "
        f"retire with evidence: {summary['retire_with_evidence']} · blocked: {summary['blocked']}",
        "",
        "| Issue | Identifier | Status | Overlap | Verdict | Problem |",
        "|---|---|---|---|---|---|",
    ]
    for row in trace["rows"]:
        lines.append(
            f"| {row['issue']} | `{row['identifier']}` | {row['status']} | {row['overlap']} | "
            f"{row['verdict']} | {'; '.join(row['problems']) or '—'} |"
        )
    lines += ["", f"Owner decision: {trace['owner_decision']}"]
    if summary["duplicate_issues"]:
        lines += ["", f"Duplicate issue numbers: {', '.join(summary['duplicate_issues'])}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", help="read the issue body from this file instead of GitHub")
    parser.add_argument("--out", help="directory for the JSON and Markdown trace")
    parser.add_argument("--print", dest="print_trace", action="store_true")
    args = parser.parse_args(argv)

    body = Path(args.body).read_text(encoding="utf-8") if args.body else issue_body()
    try:
        trace = build_trace(body)
    except ValueError as error:
        print(f"cannot trace the registry: {error}", file=sys.stderr)
        return 2

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "issue-122-p12-retirement.json").write_text(
            json.dumps(trace, indent=2, sort_keys=True), encoding="utf-8")
        rendered = render_markdown(trace)
        (out / "issue-122-p12-retirement.md").write_text(rendered, encoding="utf-8")
    else:
        rendered = render_markdown(trace)

    if args.print_trace:
        print(rendered, end="")
    summary = trace["summary"]
    print(f"rows {summary['rows']} · kept {summary['kept']} · "
          f"retire {summary['retire_with_evidence']} · blocked {summary['blocked']}")
    for row in trace["rows"]:
        if row["verdict"] == "blocked":
            print(f"  blocked: {row['issue']} — {'; '.join(row['problems'])}")
    if summary["duplicate_issues"]:
        print(f"  duplicate issue numbers: {', '.join(summary['duplicate_issues'])}")
        return 1
    return 1 if summary["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())