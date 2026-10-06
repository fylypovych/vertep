#!/usr/bin/env python3
"""Issue #82 - render the media acceptance evidence as JSON + Markdown.

The rows are produced by ``tests/test_full_media_harness.py``, which drives the
real control plane and records one evidence document per row through
``tests.media_harness.record_evidence()``. This script only verifies and renders
that document; it never invents a row.

Two rules matter for CI:

* a row without evidence is ``NOT_RUN``, never ``PASS``;
* a recorded SHA256 is re-verified against the artifact on disk, so a stale or
  edited evidence file cannot report a green row.

Usage::

    VERTEP_MEDIA_EVIDENCE=reports/media-acceptance.json \\
        python scripts/media-acceptance-report.py
    python scripts/media-acceptance-report.py --out reports/media-acceptance --require-all
    python scripts/media-acceptance-report.py --only lease,cancel
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

PASS = "PASS"
FAIL = "FAIL"
NOT_RUN = "NOT_RUN"

#: The declared acceptance rows of Issue #82, in report order.
ROWS: tuple[str, ...] = (
    "first_run",
    "approval_loops",
    "telegram_flow",
    "failure",
    "partial_publish",
    "dedup",
    "cancel",
    "stale_result",
    "lease",
    "restart",
    "backup_update",
    "evidence",
)


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _verify_files(evidence: dict[str, Any]) -> list[str]:
    """Re-check every recorded artifact digest against the file on disk."""
    problems: list[str] = []
    video = evidence.get("video") or {}
    files = video.get("files") or {}
    if not files:
        return problems
    for name, recorded in files.items():
        path = Path(name)
        if not path.is_file():
            problems.append(f"{path.name}: recorded artifact is missing on disk")
            continue
        actual = _digest(path)
        if actual != recorded:
            problems.append(
                f"{path.name}: sha256 {actual} does not match the recorded {recorded}")
    for version in video.get("versions") or []:
        recorded = version.get("sha256")
        if not recorded:
            continue
        match = next((name for name, value in files.items() if value == recorded), None)
        if match is None:
            problems.append(
                f"v{version.get('version')}: no artifact on disk matches the recorded sha256")
    return problems


def _provenance(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """The SHA/version/config the rows were produced by.

    Taken from the rows themselves so the report cannot claim a SHA that the
    harness never observed.
    """
    seen = [record.get("provenance") for record in (evidence or {}).values()
            if isinstance(record, dict) and record.get("provenance")]
    if not seen:
        return {}
    unique = {json.dumps(item, sort_keys=True) for item in seen}
    if len(unique) > 1:
        raise ValueError(
            "the evidence rows come from different runs; refusing to report one SHA")
    return seen[0]


def evaluate(evidence: dict[str, Any] | None, *,
             only: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Turn one evidence document into a per-row verdict."""
    rows: dict[str, Any] = {}
    for name in ROWS:
        if only and name not in only:
            continue
        record = (evidence or {}).get(name)
        if record is None:
            rows[name] = {"status": NOT_RUN, "detail": "no evidence recorded"}
            continue
        problems = _verify_files(record) if isinstance(record, dict) else []
        if problems:
            rows[name] = {"status": FAIL, "detail": "; ".join(problems)}
            continue
        job_id = record.get("job_id") if isinstance(record, dict) else None
        rows[name] = {"status": PASS,
                      "expected": record.get("expected"),
                      "actual": record.get("actual"),
                      "detail": f"evidence recorded for job {job_id}" if job_id
                      else "evidence recorded"}
    statuses = {row["status"] for row in rows.values()}
    if FAIL in statuses:
        summary = FAIL
    elif NOT_RUN in statuses or not rows:
        summary = NOT_RUN
    else:
        summary = PASS
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provenance": _provenance(evidence),
            "summary": summary, "rows": rows}


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("|", "\\|").replace("\n", " ")
    if len(text) > 48:
        # digests stay readable in the table; the JSON keeps the full value
        return f"{text[:16]}…{text[-8:]}"
    return text


def render_markdown(report: dict[str, Any], version: str) -> str:
    provenance = report.get("provenance") or {}
    config = (provenance.get("config") or {}).get("backends") or {}
    lines = [f"# Media acceptance - Vertep {version}",
             "",
             f"Verdict: **{report['summary']}**",
             "",
             "| Row | Verdict | Expected | Actual | Detail |",
             "| --- | --- | --- | --- | --- |"]
    for name, row in report["rows"].items():
        lines.append(f"| `{name}` | {row['status']} | {_cell(row.get('expected'))} "
                     f"| {_cell(row.get('actual'))} | {row['detail']} |")
    lines += ["", "## Run provenance", "",
              f"- SHA: `{provenance.get('sha') or 'unknown'}`",
              f"- Version: `{provenance.get('version') or version}`",
              f"- Working tree at run time: "
              f"{'dirty' if provenance.get('dirty') else 'clean'}",
              f"- Python: `{provenance.get('python') or 'unknown'}` "
              f"on `{provenance.get('platform') or 'unknown'}`"]
    if config:
        lines += ["", "## Backend configuration", "",
                  "| Slot | Backend | Configured |", "| --- | --- | --- |"]
        for slot, backend in config.items():
            lines.append(f"| {slot} | `{backend.get('backend')}` "
                         f"| {'yes' if backend.get('configured') else 'no'} |")
    lines += ["", f"Generated: {report['generated_at']}", ""]
    return "\n".join(lines)


def _load_evidence(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", default=os.environ.get("VERTEP_MEDIA_EVIDENCE", ""),
                        help="evidence JSON written by the harness")
    parser.add_argument("--out", default="", help="write <out>.json and <out>.md")
    parser.add_argument("--only", default="", help="comma separated subset of rows")
    parser.add_argument("--require-all", action="store_true",
                        help="exit non-zero unless every row is PASS")
    args = parser.parse_args(argv)

    if not args.evidence:
        parser.error("--evidence or VERTEP_MEDIA_EVIDENCE is required")
    only = tuple(part.strip() for part in args.only.split(",") if part.strip()) or None
    try:
        report = evaluate(_load_evidence(Path(args.evidence)), only=only)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (
        ROOT / "VERSION").is_file() else "unknown"

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".json").write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8")
        out.with_suffix(".md").write_text(render_markdown(report, version), encoding="utf-8")
    else:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))

    if args.require_all and report["summary"] != PASS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
