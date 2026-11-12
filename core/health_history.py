"""Journal of unhealthy readiness events with daily rotation.

Polling runs every few seconds, so healthy snapshots are not written: they carry no
diagnostic value and made the journal grow without bound. Once per UTC day the current
file becomes ``health-history.jsonl.1`` and the previous rotated file is destroyed.
"""

import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

CURRENT = "health-history.jsonl"
PREVIOUS = "health-history.jsonl.1"
STAMP = "health-history.day"
LOCK = "health-history.lock"
# Consecutive identical events closer than this belong to one incident observed by polling.
INCIDENT_GAP_SECONDS = 60


@contextmanager
def _locked(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / LOCK).open("a+") as stream:
        if os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _keep_unhealthy(source: Path, target: Path) -> None:
    """One-time migration of a journal written before rotation existed."""
    temporary = target.with_name(target.name + ".tmp")
    with source.open(encoding="utf-8", errors="replace") as reader, temporary.open("w", encoding="utf-8") as writer:
        for line in reader:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("status") != "HEALTHY":
                writer.write(json.dumps(entry, ensure_ascii=False) + "\n")
    temporary.replace(target)
    source.unlink()


def _rotate(state_dir: Path, today: date) -> None:
    stamp_path = state_dir / STAMP
    current, previous = state_dir / CURRENT, state_dir / PREVIOUS
    try:
        stamp = date.fromisoformat(stamp_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        stamp = None
    if stamp == today:
        return
    if stamp is None:
        if current.exists():
            _keep_unhealthy(current, previous)
    elif stamp == today - timedelta(days=1):
        if current.exists():
            current.replace(previous)  # destroys the older rotated day
        else:
            previous.unlink(missing_ok=True)
    else:
        current.unlink(missing_ok=True)
        previous.unlink(missing_ok=True)
    temporary = stamp_path.with_name(STAMP + ".tmp")
    temporary.write_text(today.isoformat(), encoding="utf-8")
    temporary.replace(stamp_path)


def record(state_dir: Path, entry: dict, now: datetime | None = None) -> None:
    """Rotate when the UTC day changed; append the entry only when it is not HEALTHY."""
    today = (now or datetime.now(timezone.utc)).date()
    with _locked(state_dir):
        _rotate(state_dir, today)
        if entry.get("status") == "HEALTHY":
            return
        with (state_dir / CURRENT).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_lines(state_dir: Path, now: datetime | None = None) -> list[str]:
    """Previous rotated day followed by the current day."""
    today = (now or datetime.now(timezone.utc)).date()
    with _locked(state_dir):
        _rotate(state_dir, today)
        lines: list[str] = []
        for name in (PREVIOUS, CURRENT):
            path = state_dir / name
            if path.is_file():
                lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        return lines


def _moment(entry: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None


def signature(entry: dict) -> str:
    """Same identity as the Health page: role, outcome, selection and service check tuples."""
    checks = entry.get("checks") if isinstance(entry.get("checks"), dict) else {}
    tuples = [[name, value] for name, value in checks.items()
              if isinstance(value, list) and len(value) == 2
              and (value[0] is None or isinstance(value[0], bool)) and isinstance(value[1], str)]
    return json.dumps([entry.get("role"), entry.get("status"), checks.get("required"),
                       checks.get("selected_roles"), tuples], ensure_ascii=False)


def _same_incident(older: dict, newer: dict) -> bool:
    if signature(older) != signature(newer):
        return False
    first, second = _moment(older), _moment(newer)
    return first is None or second is None or (second - first).total_seconds() <= INCIDENT_GAP_SECONDS


def incidents(lines: list[str], count: int) -> list[dict]:
    """Newest ``count`` incidents, each represented by its first original event."""
    found: list[dict] = []
    current = None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if current is not None and _same_incident(entry, current):
            current = entry
            continue
        if current is not None:
            found.append(current)
            if len(found) == count:
                return list(reversed(found))
        current = entry
    if current is not None:
        found.append(current)
    return list(reversed(found))
