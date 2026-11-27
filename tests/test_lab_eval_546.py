"""Tests for #546 (i.0.0.5.46): LAB Eval integration (Promptfoo + lm-evaluation-harness)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab.budget import BudgetLedger, PaidModelDenied
from lab.cli import build_parser
from lab.eval_runner import (
    EVAL_FORMAT,
    EvalIsolationError,
    LabEvalRunner,
    STRIPPED_API_KEYS,
    default_configs_dir,
    isolated_env,
    validate_eval_target,
)
from lab.policy import PolicyGate
from lab.reports import ReportBuilder


def test_isolated_env_disables_telemetry_and_strips_paid_api_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-paid")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    env = isolated_env({"LAB_EVAL_MODEL": "llama3.2", "OPENAI_API_KEY": "should-not-leak"})

    assert env["PROMPTFOO_DISABLE_TELEMETRY"] == "1"
    assert env["PROMPTFOO_DISABLE_UPDATE"] == "1"
    assert env["DO_NOT_TRACK"] == "1"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["LAB_EVAL_MODEL"] == "llama3.2"
    for secret_key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        assert secret_key in STRIPPED_API_KEYS
        assert secret_key not in env


def test_eval_target_blocks_paid_providers_and_external_endpoints(tmp_path):
    budget = BudgetLedger(tmp_path / "budget")

    # Local Ollama on loopback / lab host succeeds
    prov, mod, ep = validate_eval_target("ollama", "llama3.2", "http://127.0.0.1:11434", budget)
    assert (prov, mod, ep) == ("ollama", "llama3.2", "http://127.0.0.1:11434")
    assert validate_eval_target("ollama", "llama3.2", "http://ollama.lab.internal:11434", budget)[2] == "http://ollama.lab.internal:11434"

    # Paid providers blocked
    for paid in ("openai", "openai-paid", "anthropic", "gemini", "paid"):
        with pytest.raises(PaidModelDenied):
            validate_eval_target(paid, "gpt-4o", "http://127.0.0.1:11434", budget)

    # Public external URL blocked by network isolation
    with pytest.raises(EvalIsolationError):
        validate_eval_target("ollama", "llama3.2", "https://api.example.com", budget)


def test_promptfoo_and_lm_eval_runners_persist_structured_reports(tmp_path):
    state_dir = tmp_path / "state"
    gate = PolicyGate(state_dir)
    budget = BudgetLedger(state_dir / "budget")
    captured_envs: list[dict] = []

    def fake_runner(cmd, **kwargs):
        captured_envs.append(kwargs.get("env") or {})
        if "--output" in cmd:
            out_path = Path(cmd[cmd.index("--output") + 1])
            out_path.write_text(
                json.dumps({
                    "results": {
                        "results": [
                            {"success": True, "score": 1.0, "latencyMs": 320, "testCase": {"description": "uk canon"}},
                            {"success": False, "score": 0.25, "latencyMs": 410, "testCase": {"description": "rag fact"}, "error": "missing year"},
                        ]
                    }
                }),
                encoding="utf-8",
            )
        elif "--output_path" in cmd:
            out_dir = Path(cmd[cmd.index("--output_path") + 1])
            (out_dir / "results_2026.json").write_text(
                json.dumps({
                    "results": {
                        "global_mmlu_uk": {"acc,none": 0.68, "acc_norm,none": 0.71},
                        "flores_uk": {"bleu,none": 29.4},
                    }
                }),
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = LabEvalRunner(state_dir, gate, budget, runner=fake_runner)

    pf_cfg = default_configs_dir() / "promptfoo-uk-rag.yaml"
    assert pf_cfg.is_file()
    pf_report = runner.run_promptfoo(
        config_path=pf_cfg,
        provider="ollama",
        model="llama3.2",
        endpoint="http://127.0.0.1:11434",
        commit="a" * 40,
        actor="qa",
    )
    assert pf_report["format"] == EVAL_FORMAT
    assert pf_report["engine"] == "promptfoo"
    assert pf_report["telemetry_disabled"] is True
    assert pf_report["network_isolated"] is True
    assert pf_report["summary"]["total"] == 2
    assert pf_report["summary"]["passed"] == 1
    assert pf_report["summary"]["failed"] == 1
    assert pf_report["summary"]["pass_rate"] == 0.5
    assert len(pf_report["sha256"]) == 64

    lm_cfg = json.loads((default_configs_dir() / "lm-eval-uk.json").read_text(encoding="utf-8"))
    lm_report = runner.run_lm_eval(
        tasks=lm_cfg["tasks"],
        provider="ollama",
        model="llama3.2",
        endpoint="http://127.0.0.1:11434",
        limit=10,
        commit="b" * 40,
        actor="qa",
    )
    assert lm_report["format"] == EVAL_FORMAT
    assert lm_report["engine"] == "lm-evaluation-harness"
    assert lm_report["summary"]["task_count"] == 2
    assert lm_report["metrics"]["global_mmlu_uk"]["acc_norm,none"] == 0.71

    # Telemetry disabled in every subprocess env
    assert len(captured_envs) == 2
    for env in captured_envs:
        assert env["PROMPTFOO_DISABLE_TELEMETRY"] == "1"
        assert env["DO_NOT_TRACK"] == "1"

    # History persisted and exposed via ReportBuilder section "evals"
    assert len(runner.list_runs()) == 2
    assert "evals" in ReportBuilder.available()

    # CLI parser accepts eval subcommands
    parser = build_parser()
    parsed = parser.parse_args(["eval", "promptfoo", "--model", "llama3.2"])
    assert parsed.eval_action == "promptfoo"


def test_lab_compose_and_dockerfile_support_unprivileged_bwrap_eval_isolation():
    import yaml

    root = Path(__file__).resolve().parents[1]
    eval_net_src = (root / "lab" / "eval_network.py").read_text(encoding="utf-8")
    assert "--unshare-user-try" in eval_net_src
    assert "--unshare-net" in eval_net_src
    assert "--unshare-pid" in eval_net_src

    dockerfile = (root / "deploy" / "lab" / "Dockerfile").read_text(encoding="utf-8")
    assert "bubblewrap" in dockerfile and "libseccomp2" in dockerfile
    assert "USER 10001:10001" in dockerfile

    unconfined = {"seccomp=unconfined", "apparmor=unconfined"}
    for rel, services in (
        ("deploy/lab/docker-compose.yml", ("lab-control",)),
        ("deploy/lab/docker-compose.web.yml", ("lab-control", "lab-executor")),
    ):
        compose = yaml.safe_load((root / rel).read_text(encoding="utf-8"))
        # Services holding secrets keep Docker's default seccomp/AppArmor profiles.
        for svc_name in services:
            svc = compose["services"][svc_name]
            assert not svc.get("privileged")
            assert "ALL" in (svc.get("cap_drop") or [])
            sec_opts = set(svc.get("security_opt") or [])
            assert "no-new-privileges:true" in sec_opts
            assert not sec_opts & unconfined, f"{rel}:{svc_name} must keep default profiles"
        # Only the on-demand eval container drops them, and it holds no secrets or LAB state.
        evaluator = compose["services"]["lab-eval"]
        assert evaluator["profiles"] == ["eval"] and evaluator["restart"] == "no"
        assert unconfined <= set(evaluator["security_opt"]) and "no-new-privileges:true" in evaluator["security_opt"]
        assert "ALL" in evaluator["cap_drop"] and evaluator["read_only"] is True
        assert not evaluator.get("ports") and not evaluator.get("privileged")
        env = evaluator["environment"]
        names = set(env) if isinstance(env, dict) else {item.split("=", 1)[0] for item in env}
        assert not {name for name in names if "TOKEN" in name or "SECRET" in name or "GITHUB" in name}
        mounts = evaluator["volumes"]
        assert mounts == ["lab-eval-state:/var/lib/vertep-lab-eval"]
