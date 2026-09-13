"""Unit tests for scripts/qualify-infrastructure.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Import the module from scripts
from scripts.qualify_infrastructure import (
    SCENARIOS,
    collect_scenarios,
    load_version,
    git_commit,
    environment,
    parse_pytest_summary,
    proof_entry,
    build_report,
    resolve_selected,
)


def test_scenarios_catalog_not_empty():
    assert len(SCENARIOS) == 8
    for scenario in SCENARIOS:
        assert "id" in scenario
        assert "name" in scenario
        assert "automated" in scenario
        assert isinstance(scenario["automated"], list)


def test_collect_scenarios_resolves_paths():
    resolved = collect_scenarios()
    assert len(resolved) == len(SCENARIOS)
    for entry in resolved:
        for target in entry["automated"]:
            p = Path(target)
            # The target should be an absolute path under ROOT
            assert p.is_absolute()
            assert str(p).startswith(str(Path(__file__).resolve().parents[1]))


def test_load_version_returns_string():
    v = load_version()
    assert isinstance(v, str)
    # In the repo, VERSION file exists and contains something like "0.0.1.59"
    assert v != "unknown"


def test_git_commit_returns_string():
    c = git_commit()
    assert isinstance(c, str)
    # Should be a short SHA (e.g., c83be834) or "unknown"
    assert len(c) > 0


def test_environment_dict():
    env = environment()
    assert isinstance(env, dict)
    for key in ("platform", "python", "system", "is_windows", "runner"):
        assert key in env


def test_parse_pytest_summary():
    output = "============================= test session starts ==============================\ncollected 2 items\n\n..                                                                       [100%]\n\n============================== 2 passed in 0.12s ==============================="
    counts = parse_pytest_summary(output)
    assert counts.get("passed") == 2
    assert counts.get("failed", 0) == 0
    assert counts.get("error", 0) == 0


def test_proof_entry_no_targets_procedure():
    scenario = {
        "id": "SXX",
        "name": "Procedure only",
        "automated": [],
        "rt": "rt::SXX",
        "description": "desc",
    }
    entry = proof_entry(scenario, "1.0.0", "abc123", {"system": "linux"}, run=True, python="python")
    assert entry["result"] == "PROCEDURE"
    assert entry["evidence"][0]["target"] == "rt::SXX"
    assert entry["evidence"][0]["result"] == "PROCEDURE"


def test_proof_entry_not_run_when_plan():
    scenario = {
        "id": "SXX",
        "name": "Not run",
        "automated": ["tests/test_bootstrap_wizard.py"],
        "rt": None,
        "description": "desc",
    }
    entry = proof_entry(scenario, "1.0.0", "abc123", {"system": "linux"}, run=False, python="python")
    assert entry["result"] == "NOT_RUN"
    assert entry["evidence"][0]["result"] == "NOT_RUN"


def test_build_report_structure():
    scenarios = collect_scenarios()[:2]  # take first two
    report = build_report(scenarios, "1.0.0", "abc123", {"system": "linux"}, run=False, python="python")
    assert report["tool"] == "qualify-infrastructure"
    assert report["issue"] == "i.0.0.0.31"
    assert report["version"] == "1.0.0"
    assert report["commit"] == "abc123"
    assert report["mode"] == "plan"
    assert len(report["scenarios"]) == 2
    assert report["summary"]["total"] == 2
    assert report["summary"]["not_run"] == 2
    assert report["overall"] == "PLAN"


def test_resolve_selected_filters():
    all_scenarios = collect_scenarios()
    selected = resolve_selected(all_scenarios, ["S01", "S03"])
    ids = {s["id"] for s in selected}
    assert ids == {"S01", "S03"}
    assert len(selected) == 2


def test_build_report_run_counts_pass_and_fail(monkeypatch):
    """Run-режим має коректно рахувати passed/failed у summary та overall."""
    import scripts.qualify_infrastructure as qi

    fake_entries = {
        "S01": {"id": "S01", "name": "A", "description": "", "result": "PASS", "evidence": []},
        "S02": {"id": "S02", "name": "B", "description": "", "result": "FAIL", "evidence": []},
    }
    monkeypatch.setattr(
        qi, "proof_entry",
        lambda s, v, c, e, run=False, python="python": fake_entries.get(s["id"]),
    )
    scenarios = [
        {"id": "S01", "name": "A", "automated": []},
        {"id": "S02", "name": "B", "automated": []},
    ]
    report = qi.build_report(scenarios, "1.0.0", "abc123", {"system": "linux"}, run=True)
    assert report["summary"]["passed"] == 1
    assert report["summary"]["failed"] == 1
    assert report["summary"]["total"] == 2
    assert report["overall"] == "FAIL"


def test_build_report_run_procedure_overall_pass():
    """Run-режим зі сценарієм без automated (PROCEDURE) завершується PASS."""
    scenario = {
        "id": "S99",
        "name": "Procedure",
        "automated": [],
        "rt": "rt::S99",
        "description": "desc",
    }
    report = build_report([scenario], "1.0.0", "abc123", {"system": "linux"}, run=True)
    assert report["summary"]["procedure"] == 1
    assert report["summary"]["failed"] == 0
    assert report["overall"] == "PASS"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])