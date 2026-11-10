"""Small, fail-closed contract for host/runtime observations (no credentials)."""
import json
import math
import time
from pathlib import Path

SCHEMA = 1
MAX_AGE_SECONDS = 90


def read_observation(path: Path, check: str, now: float | None = None) -> tuple[bool, str]:
    try:
        if path.stat().st_size > 16384:
            raise ValueError("oversized")
        report = json.loads(path.read_text(encoding="utf-8"))
        observed = report["observed_at"]
        if type(report["schema"]) is not int or report["schema"] != SCHEMA or type(observed) not in (int, float) or not math.isfinite(observed):
            raise ValueError("invalid schema/time")
        age = (time.time() if now is None else now) - observed
        if age < 0 or age > MAX_AGE_SECONDS:
            return False, "health_observation_expired"
        result = report["checks"][check]
        if type(result) is not bool:
            raise ValueError("invalid result")
        return result, "health_probe_ok" if result else "health_probe_failed"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "health_observation_unavailable"


def write_observation(path: Path, checks: dict[str, bool]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"schema": SCHEMA, "observed_at": time.time(),
                                     "checks": checks}) + "\n", encoding="utf-8")
    temporary.replace(path)
