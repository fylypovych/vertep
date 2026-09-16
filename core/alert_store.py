"""Persistent alert store (Issue #53, #19 Monitoring block).

Replaces the derived-on-every-GET ``/api/alerts`` list with a durable entity
that has a stable id, firing/acknowledged/resolved lifecycle, acknowledge
(actor/time) and retention-controlled history.  Alerts are deduplicated by a
stable composite key so recovery resolves (and keeps) history instead of
recreating or silently erasing records.
"""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RETENTION_DAYS = int(os.getenv("ALERT_RETENTION_DAYS", "30"))
DEFAULT_MAX_ACTIVE = int(os.getenv("ALERT_MAX_ACTIVE", "500"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def alerts_path() -> Path:
    root = Path(os.getenv("CONFIG_ROOT", "/data/config"))
    return Path(os.getenv("ALERTS_FILE", str(root / "alerts.json")))


def alert_key(alert: dict) -> str:
    """Stable dedup key over the entity-scoped identity fields."""
    parts = []
    for field in ("type", "source", "job_id", "node_name", "task_id"):
        value = alert.get(field)
        if value is not None and value != "":
            parts.append(f"{field}={value}")
    return "|".join(parts)


class AlertStore:
    def __init__(self, path: str | Path | None = None,
                 retention_days: int | None = None,
                 max_active: int | None = None) -> None:
        self._path = Path(path) if path else None
        self._retention_days = retention_days if retention_days is not None else DEFAULT_RETENTION_DAYS
        self._max_active = max_active if max_active is not None else DEFAULT_MAX_ACTIVE
        self._lock = threading.Lock()
        self._alerts: list[dict] = []
        self._seq = 0
        self.load()

    # ── persistence ──────────────────────────────────────────────────────
    def _resolved_path(self) -> Path:
        return self._path or alerts_path()

    def load(self) -> None:
        path = self._resolved_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._alerts = [item for item in data if isinstance(item, dict)]
        except (OSError, ValueError):
            self._alerts = []
        self._seq = max((int(item.get("id", 0)) for item in self._alerts if str(item.get("id", "")).isdigit()), default=0)

    def save(self) -> None:
        path = self._resolved_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._alerts, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def reset(self) -> None:
        with self._lock:
            self._alerts = []
            self._seq = 0
            try:
                self._resolved_path().unlink(missing_ok=True)
            except OSError:
                pass

    # ── lifecycle ────────────────────────────────────────────────────────
    def record(self, alert: dict) -> dict:
        """Record a firing alert, deduplicating by entity key."""
        key = alert_key(alert)
        now = utc_now()
        with self._lock:
            self._prune_locked()
            if key:
                for existing in self._alerts:
                    if alert_key(existing) == key and existing.get("state") in ("firing", "acknowledged"):
                        refreshed = {field: value for field, value in alert.items() if value is not None}
                        existing.update(refreshed)
                        existing["updated_at"] = now
                        self.save()
                        return existing
            self._seq += 1
            record = {
                "id": str(self._seq),
                "severity": alert.get("severity", "warning"),
                "type": alert.get("type", "UNKNOWN"),
                "message": alert.get("message"),
                "state": "firing",
                "created_at": now,
                "updated_at": now,
            }
            for field in ("source", "job_id", "node_name", "task_id", "operation_id",
                          "state_value", "details", "severity"):
                if alert.get(field) is not None:
                    record[field] = alert[field]
            self._alerts.append(record)
            self.save()
            return record

    def resolve(self, match: dict, reason: str) -> list[dict]:
        """Resolve every firing/acknowledged alert matching ``match``. History is kept."""
        key = alert_key(match)
        if not key:
            return []
        now = utc_now()
        changed = []
        with self._lock:
            for alert in self._alerts:
                if alert.get("state") in ("firing", "acknowledged") and alert_key(alert) == key:
                    alert["state"] = "resolved"
                    alert["resolved_at"] = now
                    alert["resolved_reason"] = reason
                    alert["updated_at"] = now
                    changed.append(alert)
            if changed:
                self.save()
        return changed

    def acknowledge(self, alert_id: str, actor: str) -> dict:
        with self._lock:
            target = next((a for a in self._alerts if str(a.get("id")) == str(alert_id)), None)
            if target is None:
                raise KeyError(alert_id)
            if target.get("state") == "resolved":
                raise ValueError("cannot acknowledge a resolved alert")
            if target.get("state") == "firing":
                target["state"] = "acknowledged"
                target["acknowledged_by"] = actor
                target["acknowledged_at"] = utc_now()
                target["updated_at"] = utc_now()
                self.save()
            return target
# ── query / retention ────────────────────────────────────────────────
    def _prune_locked(self) -> None:
        now = datetime.now(timezone.utc)
        retained = []
        for alert in self._alerts:
            if alert.get("state") != "resolved":
                retained.append(alert)
                continue
            try:
                resolved_at = datetime.fromisoformat(alert.get("resolved_at", ""))
                age_days = (now - resolved_at).total_seconds() / 86400.0
            except (ValueError, TypeError):
                age_days = float("inf")
            if age_days <= self._retention_days:
                retained.append(alert)
        self._alerts = retained
        if len(self._alerts) > self._max_active:
            active = [a for a in self._alerts if a.get("state") != "resolved"]
            closed = [a for a in self._alerts if a.get("state") == "resolved"]
            overflow = len(self._alerts) - self._max_active
            self._alerts = active + closed[overflow:]

    def list(self, limit: int | None = None, state: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._alerts)
        items.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        if state:
            items = [a for a in items if a.get("state") == state]
        if limit is not None and limit > 0:
            items = items[:limit]
        return items

    def get(self, alert_id: str) -> dict | None:
        with self._lock:
            return next((a for a in self._alerts if str(a.get("id")) == str(alert_id)), None)


_store: AlertStore | None = None


def get_alert_store() -> AlertStore:
    global _store
    if _store is None:
        _store = AlertStore()
    return _store


def reset_alert_store() -> None:
    """Clear the singleton (used by test isolation in conftest)."""
    global _store
    if _store is not None:
        _store.reset()
    _store = None