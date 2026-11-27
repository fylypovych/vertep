"""Isolated AI quality evaluation runners for the laboratory (#546 / #533).

Wraps ``promptfoo`` (MIT) for prompt/RAG evaluations and ``lm-evaluation-harness``
(MIT) for Ukrainian/general LLM benchmarks inside the isolated LAB contour (#491):

* Telemetry is forcibly disabled (``PROMPTFOO_DISABLE_TELEMETRY=1``, ``DO_NOT_TRACK=1``,
  ``HF_HUB_OFFLINE=1``).
* Paid external API keys are stripped from the subprocess environment and paid
  providers are refused fail-closed via :class:`lab.budget.BudgetLedger` (§4.7).
* Target endpoints are restricted to loopback or local LAB container hosts; any
  public internet URL is refused.
* Results are normalized into ``lab_eval/v1`` reports with SHA256 digests and
  persisted under ``<state_dir>/evals/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .budget import BudgetLedger, PAID_PROVIDERS, PaidModelDenied
from .policy import Action, PolicyGate
from .state_store import append_jsonl, read_jsonl, write_json

EVAL_FORMAT = "lab_eval/v1"
SAFE_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
SAFE_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# Paid / external secret environment variables stripped from every eval subprocess.
STRIPPED_API_KEYS = frozenset({
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "COHERE_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "HUGGING_FACE_HUB_TOKEN",
    "HF_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
})

# Hosts allowed for isolated LAB evaluation endpoints.
ALLOWED_LOCAL_HOSTS = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "ollama",
    "lab-ollama",
})


class EvalIsolationError(RuntimeError):
    """Raised when an evaluation request violates LAB network/telemetry/provider policy."""


def default_configs_dir() -> Path:
    return Path(__file__).resolve().parent / "eval_configs"


def isolated_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a sanitized environment with telemetry off and paid API keys removed."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in STRIPPED_API_KEYS
    }
    env.update({
        "PROMPTFOO_DISABLE_TELEMETRY": "1",
        "PROMPTFOO_DISABLE_UPDATE": "1",
        "PROMPTFOO_DISABLE_REDTEAM_REMOTE_GENERATION": "1",
        "DO_NOT_TRACK": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "NO_PROXY": "*",
    })
    if extra:
        for key, value in extra.items():
            if key not in STRIPPED_API_KEYS:
                env[key] = str(value)
    return env


def validate_eval_target(
    provider: str,
    model: str,
    endpoint: str | None,
    budget: BudgetLedger,
) -> tuple[str, str, str]:
    """Fail-closed validation of provider, model, and local/isolated LAB endpoint."""
    prov = (provider or "").strip().lower()
    mod = (model or "").strip()
    if not prov or not mod:
        raise EvalIsolationError("Evaluation requires non-empty provider and model")
    if prov in PAID_PROVIDERS or any(
        prov.startswith(prefix) for prefix in ("openai", "anthropic", "gemini", "vertex", "bedrock", "cohere")
    ):
        raise PaidModelDenied(f"Paid or external provider '{prov}' is forbidden in LAB eval (§4.7)")
    if prov not in set(budget.allowed_providers):
        raise PaidModelDenied(f"Provider '{prov}' is not in LAB free allowlist: {budget.allowed_providers}")

    resolved_endpoint = (endpoint or os.getenv("LAB_OLLAMA_URL", "http://127.0.0.1:11434")).strip().rstrip("/")
    parsed = urlsplit(resolved_endpoint)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise EvalIsolationError(f"Invalid LAB eval endpoint: {resolved_endpoint!r}")
    if host not in ALLOWED_LOCAL_HOSTS and not host.endswith(".lab.internal"):
        raise EvalIsolationError(
            f"External host '{host}' is blocked by LAB network isolation (#491); "
            "use loopback or local LAB endpoint"
        )
    return prov, mod, resolved_endpoint


def parse_promptfoo_output(payload: dict) -> dict:
    """Normalize Promptfoo JSON output into a stable summary + case list."""
    results_root = payload.get("results") if isinstance(payload, dict) else None
    raw_items: list = []
    if isinstance(results_root, dict) and isinstance(results_root.get("results"), list):
        raw_items = results_root["results"]
    elif isinstance(results_root, list):
        raw_items = results_root

    cases: list[dict] = []
    passed = 0
    failed = 0
    total_score = 0.0
    total_latency_ms = 0.0
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        ok = bool(item.get("success"))
        score = float(item.get("score") if item.get("score") is not None else (1.0 if ok else 0.0))
        latency = float(item.get("latencyMs") or 0.0)
        if ok:
            passed += 1
        else:
            failed += 1
        total_score += score
        total_latency_ms += latency
        cases.append({
            "index": idx,
            "success": ok,
            "score": round(score, 4),
            "latency_ms": round(latency, 2),
            "description": str((item.get("testCase") or {}).get("description") or item.get("promptId") or f"case-{idx}"),
            "error": item.get("error"),
        })

    total = passed + failed
    return {
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "avg_score": round(total_score / total, 4) if total else 0.0,
            "avg_latency_ms": round(total_latency_ms / total, 2) if total else 0.0,
        },
        "cases": cases,
    }


def parse_lm_eval_output(payload: dict) -> dict:
    """Normalize lm-evaluation-harness JSON output into per-task metrics."""
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, dict):
        raw_results = {}
    metrics: dict[str, dict[str, float]] = {}
    scores: list[float] = []
    for task_name, task_metrics in sorted(raw_results.items()):
        if not isinstance(task_metrics, dict):
            continue
        cleaned: dict[str, float] = {}
        for key, value in sorted(task_metrics.items()):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cleaned[str(key)] = round(float(value), 4)
        metrics[str(task_name)] = cleaned
        for primary in ("acc_norm,none", "acc,none", "acc_norm", "acc", "exact_match,none", "exact_match"):
            if primary in cleaned:
                scores.append(cleaned[primary])
                break
    return {
        "summary": {
            "task_count": len(metrics),
            "avg_primary_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        },
        "metrics": metrics,
    }


def build_promptfoo_command(
    config_path: Path,
    output_path: Path,
    *,
    executable: str | None = None,
) -> list[str]:
    binary = executable or os.getenv("LAB_PROMPTFOO_BIN") or shutil.which("promptfoo") or "promptfoo"
    return [
        binary,
        "eval",
        "--config",
        str(config_path),
        "--output",
        str(output_path),
        "--no-cache",
        "--no-progress-bar",
    ]


def build_lm_eval_command(
    tasks: list[str],
    model: str,
    endpoint: str,
    output_dir: Path,
    *,
    limit: int | None = None,
    executable: str | None = None,
) -> list[str]:
    if not tasks:
        raise ValueError("At least one lm-evaluation-harness task is required")
    for task in tasks:
        if not SAFE_TASK_RE.fullmatch(task):
            raise ValueError(f"Invalid lm-evaluation-harness task name: {task!r}")
    binary = executable or os.getenv("LAB_LM_EVAL_BIN") or shutil.which("lm_eval") or "lm_eval"
    model_args = f"model={model},base_url={endpoint}/v1/completions,num_concurrent=1,tokenized_requests=False"
    cmd = [
        binary,
        "--model",
        "local-completions",
        "--model_args",
        model_args,
        "--tasks",
        ",".join(tasks),
        "--output_path",
        str(output_dir),
    ]
    if limit is not None and limit > 0:
        cmd.extend(["--limit", str(int(limit))])
    return cmd


class LabEvalRunner:
    """Policy-gated, telemetry-free AI evaluation runner persisted in LAB state."""

    def __init__(
        self,
        state_dir: Path,
        gate: PolicyGate,
        budget: BudgetLedger,
        runner: Callable = subprocess.run,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.evals_dir = self.state_dir / "evals"
        self.evals_dir.mkdir(parents=True, exist_ok=True)
        self.gate = gate
        self.budget = budget
        self._runner = runner

    def list_runs(self) -> list[dict]:
        return read_jsonl(self.evals_dir / "history.jsonl")

    def _record_report(
        self,
        *,
        engine: str,
        suite: str,
        provider: str,
        model: str,
        endpoint: str,
        commit: str,
        normalized: dict,
        command: list[str],
    ) -> dict:
        run_id = f"eval-{engine}-{uuid.uuid4().hex[:12]}"
        report = {
            "format": EVAL_FORMAT,
            "run_id": run_id,
            "engine": engine,
            "suite": suite,
            "commit": commit,
            "provider": provider,
            "model": model,
            "endpoint": endpoint,
            "telemetry_disabled": True,
            "network_isolated": True,
            "command": command,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **normalized,
        }
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        report["sha256"] = hashlib.sha256(canonical).hexdigest()
        artifact_path = self.evals_dir / f"{run_id}.json"
        write_json(artifact_path, report)
        append_jsonl(
            self.evals_dir / "history.jsonl",
            {
                "run_id": run_id,
                "engine": engine,
                "suite": suite,
                "commit": commit,
                "provider": provider,
                "model": model,
                "summary": report.get("summary", {}),
                "artifact": str(artifact_path),
                "sha256": report["sha256"],
                "recorded_at": report["recorded_at"],
            },
        )
        return report

    def run_promptfoo(
        self,
        *,
        config_path: Path,
        provider: str = "ollama",
        model: str = "llama3.2",
        endpoint: str | None = None,
        commit: str = "0" * 40,
        actor: str = "qa",
        timeout: int = 600,
    ) -> dict:
        self.gate.require(Action.RUN_TESTS, actor=actor, subject=f"eval:promptfoo:{config_path.name}")
        self.gate.require(Action.WRITE_REPORT, actor=actor, subject=f"eval:promptfoo:{config_path.name}")
        prov, mod, resolved_endpoint = validate_eval_target(provider, model, endpoint, self.budget)
        if not config_path.is_file():
            raise FileNotFoundError(f"Promptfoo config not found: {config_path}")
        if not SAFE_COMMIT_RE.fullmatch(commit):
            raise ValueError(f"Invalid commit SHA: {commit!r}")
        self.budget.require_model(prov, mod)

        env = isolated_env({"OLLAMA_BASE_URL": resolved_endpoint, "LAB_EVAL_MODEL": mod})
        with tempfile.TemporaryDirectory(prefix="lab-promptfoo-") as tmp_dir:
            out_file = Path(tmp_dir) / "promptfoo-results.json"
            cmd = build_promptfoo_command(config_path, out_file)
            self._runner(cmd, check=True, capture_output=True, text=True, timeout=timeout, env=env)
            raw = json.loads(out_file.read_text(encoding="utf-8"))
        normalized = parse_promptfoo_output(raw)
        return self._record_report(
            engine="promptfoo",
            suite=config_path.stem,
            provider=prov,
            model=mod,
            endpoint=resolved_endpoint,
            commit=commit,
            normalized=normalized,
            command=cmd,
        )

    def run_lm_eval(
        self,
        *,
        tasks: list[str],
        provider: str = "ollama",
        model: str = "llama3.2",
        endpoint: str | None = None,
        limit: int | None = None,
        commit: str = "0" * 40,
        actor: str = "qa",
        timeout: int = 1800,
    ) -> dict:
        suite = ",".join(tasks)
        self.gate.require(Action.RUN_TESTS, actor=actor, subject=f"eval:lm-eval:{suite}")
        self.gate.require(Action.WRITE_REPORT, actor=actor, subject=f"eval:lm-eval:{suite}")
        prov, mod, resolved_endpoint = validate_eval_target(provider, model, endpoint, self.budget)
        if not SAFE_COMMIT_RE.fullmatch(commit):
            raise ValueError(f"Invalid commit SHA: {commit!r}")
        self.budget.require_model(prov, mod)

        env = isolated_env({"OLLAMA_BASE_URL": resolved_endpoint})
        with tempfile.TemporaryDirectory(prefix="lab-lmeval-") as tmp_dir:
            out_dir = Path(tmp_dir)
            cmd = build_lm_eval_command(tasks, mod, resolved_endpoint, out_dir, limit=limit)
            self._runner(cmd, check=True, capture_output=True, text=True, timeout=timeout, env=env)
            candidates = sorted(out_dir.rglob("results*.json"))
            if not candidates:
                candidates = sorted(out_dir.rglob("*.json"))
            if not candidates:
                raise RuntimeError("lm-evaluation-harness produced no JSON results file")
            raw = json.loads(candidates[-1].read_text(encoding="utf-8"))
        normalized = parse_lm_eval_output(raw)
        return self._record_report(
            engine="lm-evaluation-harness",
            suite=suite,
            provider=prov,
            model=mod,
            endpoint=resolved_endpoint,
            commit=commit,
            normalized=normalized,
            command=cmd,
        )
