"""Run, quota and model-cost control for the laboratory.

Issue #107, stage 2: "ліміти запусків, пауза при quota/rate limit, короткий
handoff між моделями, повторні запуски без втрати стану."

The ledger is durable and deliberately dumb: it counts runs and tokens per
rolling window, refuses a run once a limit is hit (:class:`QuotaExceeded`) and
enters a controlled pause on a provider rate limit (:class:`LabPaused`).  Paid
models are refused outright, so no unsanctioned paid API can be reached even by
mistake (``AGENTS.md`` §4.6: "без платних API").
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state_store import append_jsonl, read_json, read_jsonl, write_json


class QuotaExceeded(RuntimeError):
    """A configured run/token limit for the current window is exhausted."""


class LabPaused(RuntimeError):
    """The laboratory is in a controlled pause (rate limit, quota, provider)."""


class PaidModelDenied(RuntimeError):
    """A model outside the free allow-list was requested."""


#: Free providers the laboratory may use; anything else is refused.
DEFAULT_ALLOWED_PROVIDERS = ("ollama", "omniroute-free", "local")

#: Providers that are always refused regardless of configuration.
PAID_PROVIDERS = frozenset({"openai", "anthropic", "gemini", "groq-paid", "paid"})


@dataclass
class BudgetLedger:
    """Durable counters plus free-model allow-list enforcement."""

    state_dir: Path
    max_runs_per_day: int = 24
    max_runs_per_hour: int = 4
    max_tokens_per_day: int = 400_000
    pause_until: str = ""
    allowed_providers: tuple = DEFAULT_ALLOWED_PROVIDERS
    runs: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = read_json(self.state_dir / "budget.json", default={}) or {}
        self.max_runs_per_day = int(payload.get("max_runs_per_day", self.max_runs_per_day))
        self.max_runs_per_hour = int(payload.get("max_runs_per_hour", self.max_runs_per_hour))
        self.max_tokens_per_day = int(payload.get("max_tokens_per_day",
                                                  self.max_tokens_per_day))
        self.pause_until = str(payload.get("pause_until", ""))
        providers = payload.get("allowed_providers")
        if isinstance(providers, list) and providers:
            self.allowed_providers = tuple(str(item) for item in providers)
        self.runs = read_jsonl(self.state_dir / "runs.jsonl")
        # Persisted limits must survive a restart, so assigning them writes the
        # budget file immediately instead of relying on the caller to remember.
        # Persistence stays off until __post_init__ has loaded the stored file,
        # otherwise the dataclass defaults assigned during __init__ would
        # clobber the persisted values before they are read.
        self._loaded = True

    _PERSISTED_LIMITS = ("max_runs_per_day", "max_runs_per_hour",
                         "max_tokens_per_day", "pause_until", "allowed_providers")
    _loaded = False

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name in self._PERSISTED_LIMITS and getattr(self, "_loaded", False):
            try:
                self._save_limits()
            except Exception:
                pass

    # -- persistence -----------------------------------------------------
    def _save_limits(self) -> None:
        write_json(self.state_dir / "budget.json",
                   {"max_runs_per_day": self.max_runs_per_day,
                    "max_runs_per_hour": self.max_runs_per_hour,
                    "max_tokens_per_day": self.max_tokens_per_day,
                    "pause_until": self.pause_until,
                    "allowed_providers": list(self.allowed_providers)})

    def _save_runs(self) -> None:
        """Publish the in-memory run records as the runs journal."""
        path = self.state_dir / "runs.jsonl"
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            for run in self.runs:
                output.write(json.dumps(run, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)

    # -- windows ---------------------------------------------------------
    @staticmethod
    def _within(runs: list, window: timedelta, now: datetime) -> list:
        threshold = now - window
        selected = []
        for run in runs:
            try:
                stamp = datetime.fromisoformat(str(run.get("started_at", "")))
            except ValueError:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp >= threshold:
                selected.append(run)
        return selected

    def usage(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        hourly = self._within(self.runs, timedelta(hours=1), now)
        daily = self._within(self.runs, timedelta(days=1), now)
        return {"runs_last_hour": len(hourly), "runs_last_day": len(daily),
                "tokens_last_day": sum(int(run.get("tokens", 0)) for run in daily),
                "max_runs_per_hour": self.max_runs_per_hour,
                "max_runs_per_day": self.max_runs_per_day,
                "max_tokens_per_day": self.max_tokens_per_day,
                "pause_until": self.pause_until,
                "allowed_providers": list(self.allowed_providers)}

    # -- gating ----------------------------------------------------------
    def paused(self, now: datetime | None = None) -> bool:
        if not self.pause_until:
            return False
        try:
            until = datetime.fromisoformat(self.pause_until)
        except ValueError:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if (now or datetime.now(timezone.utc)) >= until:
            self.pause_until = ""
            self._save_limits()
            return False
        return True

    def pause(self, seconds: int, reason: str = "") -> None:
        """Enter a controlled pause; the orchestrator simply stops scheduling."""
        self.pause_until = (datetime.now(timezone.utc)
                            + timedelta(seconds=max(1, int(seconds)))).isoformat()
        self._save_limits()
        append_jsonl(self.state_dir / "pauses.jsonl",
                     {"at": datetime.now(timezone.utc).isoformat(),
                      "seconds": int(seconds), "reason": reason})

    def reserve_run(self, now: datetime | None = None) -> dict:
        """Claim one run slot; raises when paused or over quota."""
        now = now or datetime.now(timezone.utc)
        if self.paused(now):
            raise LabPaused(f"laboratory is paused until {self.pause_until}")
        usage = self.usage(now)
        if usage["runs_last_hour"] >= self.max_runs_per_hour:
            raise QuotaExceeded("hourly run limit reached")
        if usage["runs_last_day"] >= self.max_runs_per_day:
            raise QuotaExceeded("daily run limit reached")
        if usage["tokens_last_day"] >= self.max_tokens_per_day:
            raise QuotaExceeded("daily token limit reached")
        record = {"started_at": now.isoformat(), "tokens": 0, "status": "running"}
        self.runs.append(record)
        append_jsonl(self.state_dir / "runs.jsonl", record)
        return record

    def record_run(self, *, tokens: int = 0, status: str = "completed",
                    detail: str = "") -> None:
        for run in reversed(self.runs):
            if run.get("status") == "running":
                run["tokens"] = int(tokens)
                run["status"] = status
                run["finished_at"] = datetime.now(timezone.utc).isoformat()
                if detail:
                    run["detail"] = detail
                break
        # The journal holds one record per run: the reserve_run line is updated
        # in place instead of being duplicated by a second, unkeyed line.
        self._save_runs()

    def pauses(self) -> list:
        """Journal of every controlled pause the laboratory has entered."""
        return read_jsonl(self.state_dir / "pauses.jsonl")

    # -- model allow-list ------------------------------------------------
    def allow_model(self, provider: str, model: str = "") -> bool:
        provider = str(provider or "").strip().lower()
        if provider in PAID_PROVIDERS:
            raise PaidModelDenied(f"paid provider {provider} is outside the allow-list")
        return provider in {item.lower() for item in self.allowed_providers}

    def require_model(self, provider: str, model: str = "") -> None:
        if not self.allow_model(provider, model):
            raise PaidModelDenied(
                f"provider {provider} is not in the free allow-list "
                f"{sorted(self.allowed_providers)}")

    def note_rate_limit(self, retry_after_seconds: int, provider: str = "") -> None:
        """Pause on a provider rate limit instead of hammering the quota."""
        self.pause(max(60, int(retry_after_seconds)),
                   reason=f"rate limit from {provider or 'provider'}")

    def records(self) -> dict:
        """Raw journals the laboratory keeps on disk."""
        from .state_store import read_jsonl
        return {"runs": read_jsonl(self.state_dir / "runs.jsonl"),
                "pauses": read_jsonl(self.state_dir / "pauses.jsonl")}


