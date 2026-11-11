"""Read deployment intent separately from the executor's applied result."""
from datetime import datetime, timezone
from pathlib import Path


def operation_state(request: dict, status: dict, path: Path) -> dict:
    correlated = bool(request) and status.get("request_id") == request.get("request_id")
    # Legacy requests had no id: their plan/role set is the correlation key.
    if request and not request.get("request_id"):
        correlated = (status.get("role") == request.get("role") and
                      status.get("additional_roles") == request.get("additional_roles"))
    value = dict(status) if not request or correlated else {
        "state": "QUEUED", "phase": "QUEUED", "request_id": request.get("request_id"),
        "additional_roles": request.get("additional_roles", []),
        "updated_at": request.get("requested_at"),
    }
    if value.get("state") in {"QUEUED", "APPLYING"}:
        try:
            stamp = datetime.fromisoformat(str(value.get("updated_at")).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
        except (ValueError, TypeError):
            age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime if path.exists() else float("inf")
        # A pull/start/model pull can take hours; a queue should be consumed promptly.
        if age > (120 if value["state"] == "QUEUED" else 14400):
            value["stalled"] = True
            value["message"] = ("Системний виконавець не прийняв запит. Перевірте службу розгортання."
                                if value["state"] == "QUEUED" else
                                "Немає завершення розгортання. Перевірте системний виконавець і його журнал.")
    return value
