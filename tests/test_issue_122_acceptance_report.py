"""Issue #122 P10: the acceptance report may not turn a gap into a pass.

§6 Evidence is explicit that a missing or skipped case is not a PASS, and that a green
SHA belonging to something else is not accepted. The report in
``scripts/issue-122-acceptance-report.py`` is the artefact P10 produces, so these tests
protect the report itself: every criterion is declared against tests that really exist, and
every verdict is derived from the evidence rather than from the intention.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "issue-122-acceptance-report.py"


@pytest.fixture(scope="module")
def report_module():
    spec = importlib.util.spec_from_file_location("issue_122_acceptance_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def collected():
    """Every node id the repository can collect, so a declared id can be verified."""
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    return {
        line.strip() for line in completed.stdout.splitlines()
        if "::" in line and not line.startswith(("=", "-"))
    }


def _identity(module) -> dict:
    return {
        "vertep_sha": "0" * 40,
        "vertep_version": "0.0.0.0",
        "worktree_dirty": False,
        "config_revision": "deadbeef",
    }


def _row(module, *, needs=(), tests=("tests/x.py::test_a",)) -> dict:
    return {"id": "probe", "title": "probe", "owner": "#122", "needs": needs,
            "tests": list(tests)}


# ---------------------------------------------------------------------------
# The declared matrix itself
# ---------------------------------------------------------------------------


def test_every_criterion_declares_an_owner_a_title_and_unique_id(report_module):
    rows = report_module.acceptance_rows()

    assert rows, "the acceptance matrix is empty"
    identifiers = [row["id"] for row in rows]
    assert len(set(identifiers)) == len(identifiers), "duplicate criterion id"
    for row in rows:
        assert row["title"].strip(), f"{row['id']} has no title"
        assert row["owner"].strip(), f"{row['id']} has no owner issue"
        assert isinstance(row["needs"], tuple)
        assert isinstance(row["tests"], tuple)


def _declared_in_source(module, node_id: str) -> bool:
    """Whether the repository really declares this test, independent of collection.

    ``pytest --collect-only`` cannot list a module whose optional harness is missing, so
    for those the file itself is the evidence: the test function has to exist in it.
    """
    path = REPO_ROOT / node_id.split("::", 1)[0]
    if not path.is_file():
        return False
    name = node_id.split("::", 1)[1].split("[", 1)[0]
    return any(
        line.strip().startswith(f"def {name}(") or line.strip().startswith(f"async def {name}(")
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_every_declared_test_exists_in_the_repository(report_module, collected):
    """A criterion may only cite evidence that exists; a typo is not a pass."""
    missing = {
        test
        for row in report_module.acceptance_rows()
        for test in row["tests"]
        if not report_module._collects(test, collected)
        and not (not report_module._module_collectable(test, collected)
                 and _declared_in_source(report_module, test))
    }

    assert not missing, f"criteria cite tests that cannot be collected: {sorted(missing)}"


def test_every_criterion_cites_at_least_one_test_or_names_a_missing_environment(report_module):
    for row in report_module.acceptance_rows():
        assert row["tests"] or row["needs"], (
            f"{row['id']} has neither automated evidence nor a stated environment "
            f"requirement, so it could never be decided"
        )


def test_a_real_stand_is_never_available_without_an_authorisation(report_module):
    assert report_module.available_capabilities()[report_module.STAND] is False
    assert report_module.STAND in report_module.capability_reasons()


# ---------------------------------------------------------------------------
# Verdicts: a gap is never a pass
# ---------------------------------------------------------------------------


def test_a_declared_test_in_an_uncollectable_module_is_not_a_code_failure(report_module):
    """An optional harness may hide a whole module; that is absent evidence, not a defect.

    Without Playwright the Browser file yields no node ids at all. Counting that as a
    failure would blame the code for a missing dependency, and counting it as green would
    be worse, so the criterion is NOT_RUN and names the file it could not collect.
    """
    verdict = report_module.evaluate_row(
        _row(report_module, needs=(report_module.BROWSER,),
             tests=("tests/test_browser_e2e.py::test_a",)),
        outcomes={}, capabilities={report_module.BROWSER: True},
        identity=_identity(report_module),
        uncollectable=("tests/test_browser_e2e.py::test_a",),
    )

    assert verdict["status"] == report_module.NOT_RUN
    assert "tests/test_browser_e2e.py" in verdict["detail"]
    assert verdict["missing_tests"] == []


def test_a_declared_test_missing_from_a_collectable_module_is_still_a_failure(report_module):
    """The exemption is for an uncollectable module, not for a mistyped test name."""
    verdict = report_module.evaluate_row(
        _row(report_module, tests=("tests/x.py::test_a",)),
        outcomes={}, capabilities={}, identity=_identity(report_module),
    )

    assert verdict["status"] == report_module.FAIL
    assert verdict["uncollectable_tests"] == []


def test_a_module_collectable_is_answered_by_any_collected_node_of_it(report_module):
    collected = {"tests/test_x.py::test_a", "tests/test_x.py::TestY::test_z"}

    assert report_module._module_collectable("tests/test_x.py::test_q", collected) is True
    assert report_module._module_collectable("tests/test_y.py::test_a", collected) is False


def test_green_tests_and_a_met_environment_make_a_criterion_pass(report_module):
    verdict = report_module.evaluate_row(
        _row(report_module),
        outcomes={"tests/x.py::test_a": "PASSED"},
        capabilities={}, identity=_identity(report_module),
    )

    assert verdict["status"] == report_module.PASS
    assert verdict["trace_id"]


def test_a_skipped_test_is_not_a_pass(report_module):
    verdict = report_module.evaluate_row(
        _row(report_module),
        outcomes={"tests/x.py::test_a": "SKIPPED"},
        capabilities={}, identity=_identity(report_module),
    )

    assert verdict["status"] == report_module.NOT_RUN
    assert "skipped" in verdict["detail"]


def test_a_test_that_never_ran_is_not_a_pass(report_module):
    verdict = report_module.evaluate_row(
        _row(report_module),
        outcomes={}, capabilities={}, identity=_identity(report_module),
    )

    assert verdict["status"] == report_module.FAIL
    assert verdict["missing_tests"] == ["tests/x.py::test_a"]


def test_a_failing_test_fails_the_criterion(report_module):
    verdict = report_module.evaluate_row(
        _row(report_module),
        outcomes={"tests/x.py::test_a": "FAILED"},
        capabilities={}, identity=_identity(report_module),
    )

    assert verdict["status"] == report_module.FAIL
    assert verdict["failed_tests"] == ["tests/x.py::test_a"]


def test_an_unmet_environment_requirement_is_not_run_with_a_cause(report_module):
    verdict = report_module.evaluate_row(
        _row(report_module, needs=(report_module.STAND,)),
        outcomes={"tests/x.py::test_a": "PASSED"},
        capabilities={report_module.STAND: False},
        identity=_identity(report_module),
        reasons={report_module.STAND: "needs the owner's authorisation"},
    )

    assert verdict["status"] == report_module.NOT_RUN
    assert "authorisation" in verdict["detail"]
    assert verdict["missing_needs"] == [report_module.STAND]


def test_a_parameterised_test_resolves_to_its_worst_outcome(report_module):
    outcomes = {
        "tests/x.py::test_a[one]": "PASSED",
        "tests/x.py::test_a[two]": "FAILED",
    }

    assert report_module._resolve_outcomes(["tests/x.py::test_a"], outcomes) == {
        "tests/x.py::test_a": "FAILED"
    }
    assert report_module._resolve_outcomes(
        ["tests/x.py::test_a"], {"tests/x.py::test_a[one]": "PASSED"}
    ) == {"tests/x.py::test_a": "PASSED"}


def test_a_junit_class_name_becomes_a_pytest_node_id(report_module):
    assert report_module._canonical_node_id(
        "tests.test_video_engines", "test_native_engine_renders_real_video"
    ) == "tests/test_video_engines.py::test_native_engine_renders_real_video"
    assert report_module._canonical_node_id(
        "tests.test_x.TestClass", "test_y"
    ) == "tests/test_x.py::TestClass::test_y"


# ---------------------------------------------------------------------------
# The report artefact
# ---------------------------------------------------------------------------


def test_the_identity_records_the_tree_the_evidence_belongs_to(report_module):
    identity = report_module.report_identity()

    for field in ("vertep_sha", "vertep_version", "pinned_upstream",
                  "bridge_schema_version", "config_revision"):
        assert identity[field] and identity[field] != "unknown", f"{field} is missing"
    assert isinstance(identity["worktree_dirty"], bool)


def test_a_dirty_worktree_is_reported_as_not_a_released_sha(report_module):
    identity = _identity(report_module) | {"worktree_dirty": True}
    verdict = report_module.evaluate_row(
        _row(report_module),
        outcomes={"tests/x.py::test_a": "PASSED"},
        capabilities={}, identity=identity,
    )

    assert verdict["status"] == report_module.PASS
    assert "dirty worktree" in verdict["detail"]


def test_a_browser_harness_that_cannot_reach_the_state_store_is_not_a_harness(
        report_module, tmp_path, monkeypatch):
    """A reachable CORE alone would let the system-state checkpoint skip silently.

    §6 Evidence: a skipped case is not a PASS. The harness therefore has to address the
    state store of the running CORE, or the Browser criterion stays NOT_RUN.
    """
    monkeypatch.setenv("VERTEP_URL", "http://127.0.0.1:8099")
    monkeypatch.delenv("VERTEP_E2E_STATE_DIR", raising=False)
    monkeypatch.setattr(report_module, "_live_server", lambda: True)
    monkeypatch.setattr(report_module, "_http_json", lambda *a, **k: (200, {"version": "0.0.0.0"}))

    assert report_module._browser_harness() == (False, report_module._browser_harness()[1])
    assert "VERTEP_E2E_STATE_DIR" in report_module._browser_harness()[1]

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("VERTEP_E2E_STATE_DIR", str(state))
    monkeypatch.setattr(report_module, "_http_json",
                        lambda *a, **k: (200, {"version": report_module._repository_version()}))

    assert report_module._browser_harness() == (True, "")


def test_the_rendered_report_refuses_to_carry_a_credential(report_module):
    assert report_module._has_secrets("x-api-key: hunter2") is True
    assert report_module._has_secrets("token: runtime-key") is True
    assert report_module._has_secrets("| recovery | **PASS** | #122 | 3 tests passed |") is False


def test_the_markdown_report_names_every_criterion_and_its_verdict(report_module):
    report = {
        "identity": report_module.report_identity(),
        "rows": [
            {"id": "contract", "title": "Contract", "owner": "#122 §6",
             "status": report_module.PASS, "detail": "green", "trace_id": "abc123"},
        ],
        "summary": {"pass": 1, "fail": 0, "not_run": 0, "declared_but_absent": []},
    }

    rendered = report_module.render_markdown(report)

    assert "| contract — Contract | **PASS** | #122 §6 | green | `abc123` |" in rendered
    assert "NOT_RUN" in rendered
    assert "не є PASS" in rendered


def test_trace_ids_are_stable_per_criterion_and_carry_nothing_identifying(report_module):
    first = report_module.sanitized_trace_id("a" * 40, "contract")
    second = report_module.sanitized_trace_id("a" * 40, "recovery")

    assert first == report_module.sanitized_trace_id("a" * 40, "contract")
    assert first != second
    assert len(first) == 12 and all(char in "0123456789abcdef" for char in first)


def test_the_published_report_can_be_written_and_read_back(report_module, tmp_path):
    report = {
        "identity": report_module.report_identity(),
        "capabilities": report_module.available_capabilities(),
        "capability_reasons": report_module.capability_reasons(),
        "rows": [],
        "summary": {"pass": 0, "fail": 0, "not_run": 0, "declared_but_absent": []},
    }

    json_path, markdown_path = report_module.write_report(report, tmp_path)

    assert json_path.is_file() and markdown_path.is_file()
    assert markdown_path.read_text(encoding="utf-8").startswith("# Vertep #122 P10")