"""Unit tests for scripts/qualify-infrastructure.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Import the module from scripts
from scripts.qualify_infrastructure import (
    SCENARIOS,
    VALID_RT_ISSUES,
    collect_scenarios,
    load_version,
    git_commit,
    environment,
    parse_pytest_summary,
    proof_entry,
    build_report,
    resolve_selected,
    validate_rt_issues,
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
    assert counts.get("skipped", 0) == 0


def test_parse_pytest_summary_counts_skipped():
    output = "18 skipped, 2 passed in 0.20s"
    counts = parse_pytest_summary(output)
    assert counts["skipped"] == 18
    assert counts["passed"] == 2


def test_parse_pytest_summary_all_skipped():
    output = "=========================== short test summary info ===========================\nSKIPPED [18] tests/test_x.py: requires openssl\n========================= 18 skipped in 0.20s =========================="
    counts = parse_pytest_summary(output)
    assert counts["passed"] == 0
    assert counts["skipped"] == 18


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


def test_build_report_run_procedure_is_partial():
    """Run-режим зі сценарієм без automated (PROCEDURE) не дає PASS."""
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
    assert report["overall"] == "PARTIAL"


def test_build_report_run_procedure_with_allow_procedures_pass():
    scenario = {
        "id": "S99",
        "name": "Procedure",
        "automated": [],
        "rt": "rt::S99",
        "description": "desc",
    }
    report = build_report(
        [scenario], "1.0.0", "abc123", {"system": "linux"}, run=True,
        allow_procedures=True,
    )
    assert report["overall"] == "PASS"


def test_build_report_run_mixed_pass_and_procedure_is_partial(monkeypatch):
    import scripts.qualify_infrastructure as qi

    def fake_entry(s, v, c, e, run=False, python="python"):
        if s["id"] == "S01":
            return {"id": "S01", "name": "A", "description": "", "result": "PASS",
                    "evidence": []}
        return {"id": "S02", "name": "B", "description": "", "result": "PROCEDURE",
                "evidence": []}

    monkeypatch.setattr(qi, "proof_entry", fake_entry)
    scenarios = [
        {"id": "S01", "name": "A", "automated": [], "rt": "rt::S01", "description": ""},
        {"id": "S02", "name": "B", "automated": ["tests/test_x.py"],
         "rt": "rt::S02", "description": ""},
    ]
    report = qi.build_report(scenarios, "1.0.0", "abc123", {"system": "linux"}, run=True)
    assert report["summary"]["passed"] == 1
    assert report["summary"]["procedure"] == 1
    assert report["overall"] == "PARTIAL"


def test_build_report_run_empty_is_fail():
    report = build_report([], "1.0.0", "abc123", {"system": "linux"}, run=True)
    assert report["summary"]["total"] == 0
    assert report["overall"] == "FAIL"


def test_build_report_run_all_pass_is_pass(monkeypatch):
    import scripts.qualify_infrastructure as qi

    scenario = {"id": "S01", "name": "A", "automated": ["tests/test_x.py"],
                "rt": "rt::S01", "description": ""}
    monkeypatch.setattr(
        qi, "proof_entry",
        lambda s, v, c, e, run=False, python="python":
            {"id": s["id"], "name": s["name"], "description": "", "result": "PASS",
             "evidence": []},
    )
    report = qi.build_report([scenario], "1.0.0", "abc123", {"system": "linux"}, run=True)
    assert report["overall"] == "PASS"


def test_proof_entry_no_tests_is_not_pass(monkeypatch):
    import scripts.qualify_infrastructure as qi

    scenario = {"id": "S01", "name": "A", "automated": ["tests/test_x.py"],
                "rt": "rt::S01", "description": ""}
    monkeypatch.setattr(
        qi, "_run_pytest",
        lambda target, python: {"target": str(target), "result": "NO_TESTS",
                                "error": "no tests executed",
                                "detail": {"passed": 0, "failed": 0, "error": 0,
                                           "skipped": 5}},
    )
    entry = qi.proof_entry(scenario, "1.0.0", "abc123", {"system": "linux"},
                           run=True, python="python")
    assert entry["result"] == "NO_TESTS"
    report = qi.build_report([scenario], "1.0.0", "abc123", {"system": "linux"}, run=True)
    assert report["summary"]["no_tests"] == 1
    assert report["overall"] == "FAIL"


def test_proof_entry_skipped_evidence_is_fail(monkeypatch):
    import scripts.qualify_infrastructure as qi

    scenario = {"id": "S01", "name": "A", "automated": ["tests/test_x.py"],
                "rt": "rt::S01", "description": ""}
    monkeypatch.setattr(
        qi, "_run_pytest",
        lambda target, python: {"target": str(target), "result": "FAIL",
                                "error": "pytest summary: passed=18 skipped=18 returncode=0",
                                "detail": {"passed": 18, "failed": 0, "error": 0,
                                           "skipped": 18}},
    )
    entry = qi.proof_entry(scenario, "1.0.0", "abc123", {"system": "linux"},
                           run=True, python="python")
    assert entry["result"] == "FAIL"


def test_validate_rt_issues_ok():
    assert validate_rt_issues() == []
    assert validate_rt_issues(SCENARIOS) == []


def test_validate_rt_issues_rejects_out_of_set():
    bad = [{"id": "S99", "rt_issue": 40}]
    problems = validate_rt_issues(bad)
    assert len(problems) == 1
    assert "S99" in problems[0]


def test_validate_rt_issues_rejects_missing():
    problems = validate_rt_issues([{"id": "S99"}])
    assert len(problems) == 1


def test_all_scenarios_have_valid_rt_issue():
    for scenario in SCENARIOS:
        assert scenario["rt_issue"] in VALID_RT_ISSUES, scenario["id"]


def test_main_plan_mode_exits_one(monkeypatch, capsys):
    import scripts.qualify_infrastructure as qi
    code = qi.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert '"overall": "PLAN"' in out


def test_main_plan_only_exits_zero(monkeypatch):
    import scripts.qualify_infrastructure as qi
    assert qi.main(["--plan-only"]) == 0


def test_main_unknown_selected_exits_one(capsys):
    import scripts.qualify_infrastructure as qi
    code = qi.main(["--selected", "S99"])
    err = capsys.readouterr().err
    assert code == 1
    assert "S99" in err


def test_main_rt_issue_violation_exits_one(monkeypatch, capsys):
    import scripts.qualify_infrastructure as qi
    bad = list(SCENARIOS)
    bad[0] = dict(bad[0], rt_issue=40)
    monkeypatch.setattr(qi, "SCENARIOS", bad)
    code = qi.main(["--plan-only"])
    err = capsys.readouterr().err
    assert code == 1
    assert "rt_issue validation" in err


if __name__ == "__main__":
    pytest.main([__file__, "-v"])