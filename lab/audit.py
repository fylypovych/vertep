"""Append-only audit trail of every policy decision.

``AGENTS.md`` §4.6 requires an audit trail of decisions that cannot be bypassed
through the CLI, an agent or the Web panel.  Each record is hash-chained to its
predecessor, so removing or editing an earlier entry breaks verification — the
same property the laboratory needs to prove that an autonomous action was
authorised before it ran.

The trail never carries secrets: only the decision, the rule that produced it
and non-sensitive metadata are stored.
"""

import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state_store import append_jsonl, read_jsonl, write_json

GENESIS = "0" * 64

#: Keys whose values must never reach the audit file.  The trail records
#: decisions, not credentials (``AGENTS.md`` §12).
REDACTED_KEYS = ("token", "secret", "password", "passphrase", "key", "credential",
                 "authorization", "cookie")


def _canonical(record: dict) -> str:
    parts = [str(record.get(key, "")) for key in
             ("sequence", "timestamp", "actor", "action", "subject", "decision",
              "rule", "reason", "previous_hash")]
    return "|".join(parts)


def redact(extra: dict | None) -> dict:
    """Mask credential-looking values before they are persisted."""
    if not extra:
        return {}
    safe = {}
    for key, value in extra.items():
        name = str(key).lower()
        safe[str(key)] = "***redacted***" if any(marker in name
                                                 for marker in REDACTED_KEYS) else str(value)
    return safe



class AuditTrail:
    """Hash-chained JSONL journal of gate decisions."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    # -- reading ---------------------------------------------------------
    def records(self) -> list[dict]:
        return read_jsonl(self.path)

    def _last_hash(self) -> str:
        records = self.records()
        return str(records[-1].get("hash") or GENESIS) if records else GENESIS

    def _next_sequence(self) -> int:
        records = self.records()
        return int(records[-1].get("sequence", 0)) + 1 if records else 1

    # -- writing ---------------------------------------------------------
    def append(self, *, actor: str, action: str, subject: str, decision: str,
               rule: str, reason: str = "", extra: dict | None = None) -> dict:
        """Record one decision and return the stored, chained record."""
        with self._lock:
            previous = self._last_hash()
            record: dict[str, Any] = {
                "sequence": self._next_sequence(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "actor": str(actor),
                "action": str(action),
                "subject": str(subject),
                "decision": str(decision),
                "rule": str(rule),
                "reason": str(reason),
                "previous_hash": previous,
            }
            if extra:
                record["extra"] = redact(extra)
            record["hash"] = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
            append_jsonl(self.path, record)
            return record

    # -- verification ----------------------------------------------------
    def verify(self) -> dict:
        """Return a fail-closed integrity report of the whole trail."""
        problems: list[str] = []
        records = self.records()
        previous = GENESIS
        for index, record in enumerate(records, start=1):
            if int(record.get("sequence", 0) or 0) != index:
                problems.append(f"sequence gap at record {index}")
            if str(record.get("previous_hash", "")) != previous:
                problems.append(f"broken chain at record {index}")
            expected = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
            if str(record.get("hash", "")) != expected:
                problems.append(f"tampered record {index}")
            previous = str(record.get("hash", ""))
        return {"records": len(records), "valid": not problems, "problems": problems,
                "path": str(self.path)}


def snapshot_pointer(path: Path, trail: AuditTrail) -> None:
    """Persist the trail head so a restart can detect truncation."""
    records = trail.records()
    write_json(path, {"records": len(records),
                      "head": records[-1].get("hash") if records else GENESIS})
