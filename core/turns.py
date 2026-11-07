"""Turns between identical idle workers (#432).

A worker pulls its own task, so CORE spreads work by asking a worker to skip a claim while an
identical idle worker has waited longer. The order is kept in the worker record: a CORE that
lost it starts a new round, and the first claim of that round goes to whoever asks first.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from .first_run import config_root

_SETTINGS = "dispatch-settings.json"
DEFAULT_TURN_TIMEOUT = 45

# ponytail: one process per CORE, as the worker registry itself; shared store if CORE ever scales out.
_yielding_since: dict[str, float] = {}


def turn_timeout_seconds() -> int:
    """Set in Settings -> System."""
    try:
        value = json.loads((config_root() / _SETTINGS).read_text(encoding="utf-8"))["worker_turn_timeout_seconds"]
        return value if isinstance(value, int) and not isinstance(value, bool) else DEFAULT_TURN_TIMEOUT
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_TURN_TIMEOUT


def save_turn_timeout_seconds(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 600:
        raise ValueError("Час очікування має бути цілим числом від 0 до 600 секунд")
    path = config_root() / _SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{_SETTINGS}.tmp")
    temporary.write_text(json.dumps({"worker_turn_timeout_seconds": value}), encoding="utf-8")
    os.replace(temporary, path)
    return value


def _identity(worker: dict) -> tuple:
    """What makes two workers interchangeable for any task."""
    return (worker.get("role"), tuple(sorted(worker.get("capabilities") or [])),
            tuple(sorted(worker.get("supported_tasks") or [])),
            tuple(sorted(worker.get("supported_workflows") or [])),
            tuple(sorted(worker.get("scheduler_tags") or [])),
            worker.get("gpu_name"), worker.get("vram_mb"))


def _idle_and_alive(worker: dict, now: float) -> bool:
    if worker.get("current_task") or worker.get("desired_state") or worker.get("status") in {"BUSY", "OFFLINE"}:
        return False
    try:
        seen = datetime.fromisoformat(str(worker["last_seen"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return now - seen.timestamp() <= int(os.getenv("HEARTBEAT_TIMEOUT", "45"))


def may_claim_now(node_name: str, workers: dict, now: float | None = None) -> bool:
    """False only while an identical idle worker is next, and only for a bounded time."""
    now = time.time() if now is None else now
    me = workers.get(node_name)
    if not me:
        return True
    mine = me.get("scheduler_last_claim_at") or 0
    waiting_longer = mine and any(
        name != node_name and _identity(other) == _identity(me) and _idle_and_alive(other, now)
        and (other.get("scheduler_last_claim_at") or 0) < mine
        for name, other in workers.items())
    if not waiting_longer:
        _yielding_since.pop(node_name, None)
        return True
    # The other worker may have hung while its heartbeat lives on: work must not wait for it.
    since = _yielding_since.setdefault(node_name, now)
    if now - since >= turn_timeout_seconds():
        _yielding_since.pop(node_name, None)
        return True
    return False


def record_claim(node_name: str, workers: dict) -> None:
    _yielding_since.pop(node_name, None)
    workers[node_name]["scheduler_last_claim_at"] = time.time()
