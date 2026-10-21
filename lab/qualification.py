"""Evidence model of the laboratory qualification matrix (Issue #252).

Issue ``ir.0.0.2.52`` requires a *real* qualification of the laboratory on an
isolated Ubuntu Server 24.04 VM with a production-priority dispatcher and a
physical GPU.  Rows R1–R6 are only proven by observations collected on that
stand; a unit test, a browser run or a literal evidence dictionary proves
nothing.

This module is the machine-checkable half of the procedure:

* :data:`CRITERIA` — the canonical R1–R6 catalog: expected result, mandatory
  facts and the owner right each row depends on;
* the evidence row schema (:func:`build_row`) and its validator
  (:func:`validate_row`, :func:`validate_evidence`);
* :func:`overall_status` — the single rule that decides whether the matrix is
  proven.

Two invariants shape everything here:

* nothing is invented — a fact that cannot be observed is reported as
  ``BLOCKED`` together with the concrete missing prerequisite;
* only a complete, independently verified row counts as proof — mocked,
  skipped, truncated or incomplete rows are rejected.

The module is free of any Vertep CORE import: it validates evidence and
describes what has to be collected, it never touches production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

TOOL = "qualify-lab"
ISSUE = "ir.0.0.2.52"
ISSUE_NUMBER = 252

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"
STATUS_SKIPPED = "SKIPPED"
STATUS_MOCK = "MOCK"

STATUSES = (STATUS_PASS, STATUS_FAIL, STATUS_BLOCKED, STATUS_SKIPPED, STATUS_MOCK)
NON_PROOF_STATUSES = (STATUS_SKIPPED, STATUS_MOCK)

_SEVERITY = {
    STATUS_PASS: 0,
    STATUS_SKIPPED: 1,
    STATUS_MOCK: 1,
    STATUS_BLOCKED: 2,
    STATUS_FAIL: 3,
}

_PLACEHOLDER_VALUES = {
    "", "-", "none", "null", "nil", "n/a", "na", "***", "<redacted>", "[redacted]",
    "redacted", "<masked>", "[masked]", "masked", "unset", "not set", "unknown",
}


@dataclass(frozen=True)
class Criterion:
    """One row of the qualification matrix."""

    id: str
    title: str
    expected: str
    required_facts: tuple[str, ...]
    required_rights: tuple[str, ...]
    source: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "expected": self.expected,
            "required_facts": list(self.required_facts),
            "required_rights": list(self.required_rights),
            "source": self.source,
        }


#: Facts every row has to carry inside its ``facts`` block (Issue #252, R6).
COMMON_FACTS: tuple[str, ...] = (
    "version",
    "git_sha",
    "image_digests",
    "environment",
    "audit",
    "metrics",
)

CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        id="R1",
        title="Ізольована Ubuntu 24.04 VM: ресурси, життєвий цикл, відсутність production-доступу",
        expected=(
            "Окрема Ubuntu Server 24.04 VM із Docker/systemd: незалежні start/stop/restart, "
            "виміряні CPU/RAM/SSD або погоджені ресурси, status усередині контейнера; "
            "production socket/DB/volumes/secrets/config недоступні у негативних тестах; "
            "мережа лише allowlisted endpoints; без GPU passthrough."
        ),
        required_facts=("resources", "isolation", "network", "gpu_passthrough", "container"),
        required_rights=(
            "owner: deploy лабораторії на окрему Ubuntu Server 24.04 VM",
            "owner: доступ до systemd/Docker журналів цієї VM",
        ),
        source="#107 «Затверджене розміщення», isolation criteria 1–3, Етап 5.4–5.7",
    ),
    Criterion(
        id="R2",
        title="Production-priority Dispatcher/GPU: lease, queue, revoke, VRAM release",
        expected=(
            "На реальному production-priority Dispatcher/GPU через контрольований API: deny без "
            "lease, queue/reservation під час тримання lease, heartbeat/revoke при надходженні "
            "production task; виміряні control-stop/cancel latency та VRAM release/ack до передачі "
            "ресурсу production; fake provider не зараховується."
        ),
        required_facts=("lease", "vram", "latency"),
        required_rights=(
            "owner: доступ до production-priority Dispatcher/GPU endpoint",
            "owner: дозвіл на lease-операції лабораторії на реальному GPU",
        ),
        source="#107 isolation/GPU criteria 4–5, Етап 4.1–4.4, 4.7",
    ),
    Criterion(
        id="R3",
        title="Fault-сценарії: hang, connection loss, restart, quota exhaustion",
        expected=(
            "Реальні inference hang, connection loss, VM/Dispatcher restart, quota exhaustion: "
            "safe pause, жодного GPU без lease, release старого lease/reacquire без overlap, "
            "finally-release після failure/PolicyDenied; production не постраждав."
        ),
        required_facts=("lease", "faults", "vram"),
        required_rights=(
            "owner: дозволені fault-операції на окремому стенді",
            "owner: право на restart Dispatcher/VM під час кваліфікації",
        ),
        source="#107 isolation/GPU criterion 6, Етап 4.5–4.6",
    ),
    Criterion(
        id="R4",
        title="Перший actual audit на VM в ізольованій Git copy",
        expected=(
            "Перший actual audit на VM в ізольованій Git copy: static checks до AI, QA запускає "
            "тести, незалежний reviewer перевіряє, security/backend/architect звіряють погоджені "
            "вимоги; реальні дозволені model routes і handoff/restart доведені; confirmed findings "
            "проходять actual gate без несанкціонованих GitHub/code-publication effects."
        ),
        required_facts=("audit_trail", "review", "publication"),
        required_rights=(
            "owner: дозволені model routes для лабораторії",
            "owner: право на перший actual audit в ізольованій Git copy на VM",
        ),
        source="#107 Етап 6.1–6.7, початковий «Реальна перевірка»",
    ),
    Criterion(
        id="R5",
        title="24/7 режим: schedule, budgets, fallback, production-priority, звітність",
        expected=(
            "Лише після R1–R4 та окремої явної owner activation/grants: контрольований 24/7 "
            "schedule, виміряні budgets, дозволений free fallback/pauses, production-priority під "
            "навантаженням, configurable reports/notifications через погоджені UI/API."
        ),
        required_facts=("activation", "schedule", "budgets", "notifications"),
        required_rights=(
            "owner: явна 24/7 activation лабораторії",
            "owner: grants на довготривалі модельні й GPU-операції",
        ),
        source="#107 Етап 7.1–7.6, «Затверджене рішення» п.6",
    ),
    Criterion(
        id="R6",
        title="Evidence: повнота, хеші, незалежна верифікація",
        expected=(
            "Для кожного real row: exact version, повний SHA, digests, "
            "VM/OS/hardware/topology/accounts без secrets, expected/actual, timestamps, "
            "audit/metrics/VRAM, artifacts/hashes та independent verification; недоступний, "
            "failed, mock або skipped row не PASS."
        ),
        required_facts=("rows",),
        required_rights=(
            "owner: незалежна верифікація доказів окремим виконавцем",
        ),
        source="#107 «Реалізація й acceptance», real evidence/independent verification",
    ),
)

CRITERIA_BY_ID: dict[str, Criterion] = {item.id: item for item in CRITERIA}
CRITERION_IDS: tuple[str, ...] = tuple(item.id for item in CRITERIA)

REQUIRED_FACTS: dict[str, tuple[str, ...]] = {
    item.id: tuple(COMMON_FACTS) + item.required_facts for item in CRITERIA
}

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
    ("connection_string", re.compile(r"(?i)\bpostgres(?:ql)?://[^\s:@/]+:[^\s@/]+@")),
    ("assigned_secret", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token)"
        r"\s*[:=]\s*\"?([^\s,\"'}\]]{8,})\"?")),
)

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return False
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return str(value).strip().lower() in _PLACEHOLDER_VALUES


def _is_iso_utc(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def find_secret(text: str) -> str | None:
    """Return the name of the first secret-looking pattern, or ``None``."""
    for name, pattern in _SECRET_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        if name == "assigned_secret":
            captured = match.group(1).strip().strip("\"'")
            if captured.lower() in _PLACEHOLDER_VALUES:
                continue
        return name
    return None


def build_row(
    criterion_id: str,
    *,
    status: str,
    actual: str,
    facts: dict | None = None,
    artifacts: list | None = None,
    verification: dict | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    expected: str | None = None,
    notes: str = "",
) -> dict:
    """Assemble one evidence row of the qualification matrix.

    The expected text defaults to the catalog value so a row can never drift
    away from the criterion it claims to prove.
    """
    criterion = CRITERIA_BY_ID.get(criterion_id)
    if criterion is None:
        raise KeyError(f"unknown criterion: {criterion_id}")
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "criterion": criterion.id,
        "title": criterion.title,
        "status": str(status or "").upper(),
        "expected": expected if expected is not None else criterion.expected,
        "actual": str(actual or ""),
        "started_at": started_at or now,
        "finished_at": finished_at or now,
        "facts": dict(facts or {}),
        "artifacts": list(artifacts or []),
        "verification": dict(verification or {}),
        "notes": notes,
    }
    return row


def validate_row(row: dict, *, criteria: Iterable[Criterion] = CRITERIA) -> list[str]:
    """Return the list of reasons why the row is not proof (empty list = valid)."""
    catalog = {item.id: item for item in criteria}
    problems: list[str] = []
    if not isinstance(row, dict):
        return ["row is not an object"]

    criterion_id = str(row.get("criterion") or "")
    criterion = catalog.get(criterion_id)
    if criterion is None:
        return [f"unknown criterion: {criterion_id!r}"]

    status = str(row.get("status") or "").upper()
    if status not in STATUSES:
        problems.append(f"{criterion_id}: unknown status {status!r}")
        return problems

    expected = str(row.get("expected") or "")
    if expected != criterion.expected:
        problems.append(f"{criterion_id}: expected text does not match the catalog")

    actual = str(row.get("actual") or "")
    if _is_placeholder(actual):
        problems.append(f"{criterion_id}: actual result is empty")

    facts = row.get("facts")
    if not isinstance(facts, dict):
        facts = {}
        problems.append(f"{criterion_id}: facts block is missing")

    artifacts = row.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
        problems.append(f"{criterion_id}: artifacts block is missing")

    verification = row.get("verification")
    if not isinstance(verification, dict):
        verification = {}
        problems.append(f"{criterion_id}: verification block is missing")

    if status == STATUS_PASS:
        for key in REQUIRED_FACTS.get(criterion_id, ()):
            if _is_placeholder(facts.get(key)):
                problems.append(f"{criterion_id}: mandatory fact '{key}' is missing or empty")
        if not artifacts:
            problems.append(f"{criterion_id}: a passing row needs at least one hashed artifact")
        if not _is_iso_utc(row.get("started_at")):
            problems.append(f"{criterion_id}: started_at is not an ISO-8601 UTC timestamp")
        if not _is_iso_utc(row.get("finished_at")):
            problems.append(f"{criterion_id}: finished_at is not an ISO-8601 UTC timestamp")
        version = str(facts.get("version") or "")
        if not _VERSION_RE.fullmatch(version):
            problems.append(f"{criterion_id}: version '{version}' is not a full A.B.C.D version")
        sha = str(facts.get("git_sha") or "")
        if not _SHA_RE.fullmatch(sha):
            problems.append(f"{criterion_id}: git_sha '{sha}' is not a full 40-hex SHA")
        digests = facts.get("image_digests")
        if _is_placeholder(digests):
            problems.append(f"{criterion_id}: image/artifact digests are missing")
        verifier = str(verification.get("verifier") or "")
        if _is_placeholder(verifier):
            problems.append(f"{criterion_id}: independent verifier is missing")
        elif verifier == str(verification.get("collector") or ""):
            problems.append(f"{criterion_id}: verifier and collector are the same actor")
        if _is_placeholder(verification.get("method")):
            problems.append(f"{criterion_id}: verification method is missing")
    else:
        if status in NON_PROOF_STATUSES:
            problems.append(f"{criterion_id}: status '{status}' is never a proof")

    for artifact in artifacts:
        if not isinstance(artifact, dict):
            problems.append(f"{criterion_id}: malformed artifact entry")
            continue
        digest = str(artifact.get("sha256") or "")
        if digest and not _SHA256_RE.fullmatch(digest):
            problems.append(f"{criterion_id}: artifact '{artifact.get('path')}' has a broken sha256")
        elif not digest and artifact.get("exists") is not False:
            problems.append(
                f"{criterion_id}: artifact '{artifact.get('path')}' has no valid sha256")

    secret = find_secret(str(row))
    if secret:
        problems.append(f"{criterion_id}: evidence contains a secret-looking value ({secret})")

    return problems


def _overall_from_verdict(verdict: dict) -> str:
    """The matrix verdict of an already validated evidence set."""
    statuses = [status for status in verdict["statuses"].values() if status]
    if not statuses:
        return "INCOMPLETE"
    worst = max(statuses, key=lambda item: _SEVERITY.get(item, 1))
    if worst == STATUS_FAIL:
        return "FAIL"
    if worst == STATUS_BLOCKED:
        return "BLOCKED"
    if worst in NON_PROOF_STATUSES:
        return "INCOMPLETE"
    if verdict["problems"] or verdict["missing"]:
        return "INCOMPLETE"
    return STATUS_PASS


def validate_evidence(rows: list, *, criteria: Iterable[Criterion] = CRITERIA) -> dict:
    """Validate the whole matrix and return the verdict."""
    catalog = list(criteria)
    problems: list[str] = []
    seen: dict[str, int] = {}
    for row in rows or []:
        criterion_id = str((row or {}).get("criterion") or "")
        seen[criterion_id] = seen.get(criterion_id, 0) + 1
        problems.extend(validate_row(row, criteria=catalog))

    missing = [item.id for item in catalog if item.id not in seen]
    for criterion_id, count in seen.items():
        if count > 1:
            problems.append(f"{criterion_id}: {count} rows for one criterion")

    statuses = {str((row or {}).get("criterion") or ""):
                str((row or {}).get("status") or "").upper() for row in rows or []}

    verdict = {
        "problems": problems,
        "missing": missing,
        "statuses": statuses,
    }
    verdict["overall"] = _overall_from_verdict(verdict)
    return verdict


def overall_status(rows: list, *, criteria: Iterable[Criterion] = CRITERIA) -> str:
    """``PASS`` only when every criterion has a complete, verified passing row."""
    return _overall_from_verdict(validate_evidence(rows, criteria=list(criteria)))


def summarize(rows: list) -> dict:
    """Counts per status for the report header."""
    mapping = {STATUS_PASS: "passed", STATUS_FAIL: "failed", STATUS_BLOCKED: "blocked",
               STATUS_SKIPPED: "skipped", STATUS_MOCK: "mock"}
    summary = {key: 0 for key in mapping.values()}
    for row in rows or []:
        status = str((row or {}).get("status") or "").upper()
        key = mapping.get(status)
        if key:
            summary[key] += 1
    summary["total"] = sum(summary[key] for key in mapping.values())
    return summary


def build_document(rows: list, *, context: dict | None = None) -> dict:
    """Assemble the machine-readable evidence document."""
    verdict = validate_evidence(rows)
    document = {
        "tool": TOOL,
        "issue": ISSUE,
        "issue_number": ISSUE_NUMBER,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "criteria": [item.as_dict() for item in CRITERIA],
        "rows": list(rows or []),
        "summary": summarize(rows),
        "problems": verdict["problems"],
        "missing": verdict["missing"],
        "overall": verdict["overall"],
    }
    if context:
        document["context"] = context
    return document
