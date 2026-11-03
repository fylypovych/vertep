"""Issue #122 P10: the acceptance report may not turn a gap into a pass.

§6 Evidence is explicit that a missing or skipped case is not a PASS, and that a green
SHA belonging to something else is not accepted. The report in
``scripts/issue-122-acceptance-report.py`` is the artefact P10 produces, so these tests
protect the report itself: every criterion is declared against tests that really exist, and
every verdict is derived from the evidence rather than from the intention.
"""

from __future__ import annotations

import importlib.util
import json
import re
import xml.etree.ElementTree as ET
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


def test_an_engine_that_cannot_be_constructed_is_an_absent_runtime(report_module, monkeypatch):
    """A host without a writable config root renders the report instead of crashing."""
    import adapters.providers.video_engines as engines

    def unavailable(*_args, **_kwargs):
        raise PermissionError("[Errno 13] Permission denied: '/data'")

    monkeypatch.setattr(engines, "MoneyPrinterEngine", unavailable)
    assert report_module.available_capabilities()[report_module.RUNTIME] is False
    assert report_module.STAND in report_module.capability_reasons()


def _function_source(path: Path, name: str) -> str:
    """The source of a top-level function, up to the next top-level definition."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.startswith((f"def {name}(", f"async def {name}("))
    )
    body: list[str] = []
    for line in lines[start:]:
        if body and line.startswith(("def ", "async def ", "class ", "@")):
            break
        body.append(line)
    return "\n".join(body)


def test_declared_browser_evidence_does_not_mock_the_tested_settings_mutation(report_module):
    """#128 §4: a declared Browser test may not substitute ``page.route`` for the
    Settings mutation it is supposed to prove.

    The engine screen is proven against the running CORE (``page.request`` to the real
    API); a route handler that fulfils ``/api/settings/providers`` would make the test
    green against a browser-only fake and prove nothing about persistence or the Worker.
    The repoint-runtime behaviour is proven without a browser in the ``configuration``
    row, so it must not reappear here as mocked evidence.
    """
    forbidden = re.compile(r"page\.route\(\s*[\"'][^\"']*api/settings")
    offenders: list[str] = []

    for row in report_module.acceptance_rows():
        if report_module.BROWSER not in row["needs"]:
            continue
        for node_id in row["tests"]:
            path = REPO_ROOT / node_id.split("::", 1)[0]
            name = node_id.split("::", 1)[1].split("[", 1)[0]
            if path.is_file() and forbidden.search(_function_source(path, name)):
                offenders.append(node_id)

    assert not offenders, (
        f"Browser evidence mocks the tested Settings mutation: {sorted(offenders)}"
    )


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


def test_a_changed_path_is_reported_with_every_character(report_module, monkeypatch):
    """The changed paths name the files of the tree the evidence belongs to.

    ``git status --porcelain`` prefixes every line with its status characters, so
    dropping leading whitespace before reading them truncates the first path by one
    character and the report would name a file that does not exist.
    """
    monkeypatch.setattr(report_module, "_git", lambda *args: (
        " M scripts/issue-122-acceptance-report.py\n"
        "?? worker/role_executor.py\n"
        if args[:1] == ("status",) else "0" * 40
    ))

    assert report_module._dirty_paths() == [
        "scripts/issue-122-acceptance-report.py",
        "worker/role_executor.py",
    ]


def test_a_dirty_worktree_is_reported_as_not_a_released_sha(report_module):
    identity = _identity(report_module) | {"worktree_dirty": True}
    verdict = report_module.evaluate_row(
        _row(report_module),
        outcomes={"tests/x.py::test_a": "PASSED"},
        capabilities={}, identity=identity,
    )

    assert verdict["status"] == report_module.PASS
    assert "dirty worktree" in verdict["detail"]


def test_unit_evidence_cannot_be_labelled_pinned_when_a_runtime_is_available(report_module):
    row = next(row for row in report_module.acceptance_rows() if row["id"] == "recovery")
    verdict = report_module.evaluate_row(row,
        outcomes={test: "PASSED" for test in row["tests"]},
        capabilities={report_module.RUNTIME: True}, identity=_identity(report_module))
    assert verdict["status"] == report_module.PASS
    assert verdict["evidence_kind"] == "unit_or_disposable"
    pinned = next(row for row in report_module.acceptance_rows() if row["id"] == "real_submit_route")
    unavailable = report_module.evaluate_row(pinned, outcomes={}, capabilities={},
        identity=_identity(report_module))
    assert unavailable["status"] == report_module.NOT_RUN
    assert unavailable["evidence_kind"] == "pinned_runtime"


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


# ---------------------------------------------------------------------------
# Pinned M1–M4 evidence (#392 M5): a synthetic bundle must still fail closed
# ---------------------------------------------------------------------------

PINNED_SHA = "0" * 40
PINNED_CONFIG_REVISION = "deadbeef"
PINNED_DIGEST = "sha256:" + "a" * 64
MATRIX_MODULE = "tests.test_mpt_media_matrix"


def _matrix_case_name(node_id: str) -> str:
    return node_id.split("::", 1)[1]


def _junit(path: Path, cases: dict[str, str]) -> Path:
    root = ET.Element("testsuite", name="pytest")
    for name, outcome in cases.items():
        case = ET.SubElement(root, "testcase", classname=MATRIX_MODULE, name=name)
        if outcome == "FAILED":
            ET.SubElement(case, "failure", message="failed")
        elif outcome == "SKIPPED":
            ET.SubElement(case, "skipped", message="not applicable")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _matrix(report_module, **overrides) -> dict[str, str]:
    cases = {
        "test_the_approved_timeline_reaches_the_runtime_in_scene_order": "PASSED",
        _matrix_case_name(report_module.VOICE_STAGING_NEGATIVE_TEST): "PASSED",
        _matrix_case_name(report_module.CORRUPT_DOWNLOAD_TEST): "PASSED",
    }
    cases.update(overrides)
    return cases


def _snapshot(report_module, **overrides) -> dict:
    body = {
        "vertep_sha": PINNED_SHA,
        "vertep_version": "0.0.0.0",
        "config_revision": PINNED_CONFIG_REVISION,
        "image_digest": PINNED_DIGEST,
        "runtime": {
            "upstream_commit": report_module.PINNED_UPSTREAM_COMMIT,
            "bridge_schema_version": report_module.BRIDGE_SCHEMA_VERSION,
        },
        "self_test": {"status": "passed", "checks": {
            "submit_route": {
                "task_id": "task-1", "submit_key": "key-1", "bytes": 4096,
                "duration_seconds": 9.0, "width": 486, "height": 864,
                "aspect": "9:16", "fit_mode": "crop", "concat_mode": "sequential",
                "scene_order": ["scene-1", "scene-2"],
                "expected_scene_order": ["scene-1", "scene-2"],
            },
            "media_pipeline": {
                "bytes": 2048, "duration_seconds": 9.0, "aspect": "9:16",
                "fit_mode": "crop", "scene_order": ["scene-1", "scene-2"],
                "expected_scene_order": ["scene-1", "scene-2"],
            },
        }},
    }
    body.update(overrides)
    return body


def _task(*ids: str) -> list[dict]:
    return [{"task_id": task_id} for task_id in ids]


def _recovery(report_module, *, assertions=None, **overrides) -> dict:
    checks = assertions if assertions is not None else [
        *report_module.M3_REQUIRED_ASSERTIONS,
        "a busy DELETE answers 409 instead of a false cancel",
        "the approved voice is still staged after the restart",
    ]
    submitted = {"status": 200, "body": {"status": "success",
                                         "data": {"task_id": "task-1"}}}
    body = {
        "issue": 389,
        "sha": PINNED_SHA,
        "image_digest": PINNED_DIGEST,
        "result": "PASS",
        "assertions": [item if isinstance(item, dict) else {"check": item, "passed": True}
                       for item in checks],
        "first_submit": {**submitted, "tasks": _task("task-1")},
        "retried_submit": {**submitted, "tasks": _task("task-1")},
        "reconciled_after_restart": {**submitted, "tasks": _task("task-1")},
        "resubmitted_after_restart": {**submitted, "tasks": _task("task-1")},
        "tampered_voice": {"status": 503, "tasks_before": _task("task-1"),
                           "tasks": _task("task-1")},
        "missing_voice": {"refused": {"status": 503},
                          "tasks_before": _task("task-1"),
                          "retried": {**submitted, "tasks": _task("task-1")}},
        "cancel": {"status": 409, "absent_status": 404},
        "unknown_key": {"body": {"status": "success", "data": {"task_id": ""}}},
    }
    body.update(overrides)
    return body


def _bundle(tmp_path: Path, report_module, *, snapshot=None, recovery=None,
            matrix=None, omit: tuple[str, ...] = ()) -> Path:
    directory = tmp_path / "pinned-evidence"
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        report_module.PINNED_SNAPSHOT:
            snapshot if snapshot is not None else _snapshot(report_module),
        report_module.PINNED_RECOVERY:
            recovery if recovery is not None else _recovery(report_module),
        report_module.PINNED_MEDIA_MATRIX:
            matrix if matrix is not None else _matrix(report_module),
    }
    for name, payload in files.items():
        if name in omit:
            continue
        path = directory / name
        if name.endswith(".xml"):
            _junit(path, payload)
        else:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return directory


def _pinned(module, directory: Path | None) -> tuple[dict, dict[str, dict]]:
    identity = _identity(module)
    evidence = module.load_pinned_evidence(directory, identity=identity)
    rows = module.evaluate_pinned_criteria(evidence, identity=identity)
    return evidence, {row["id"]: row for row in rows}


def _stand_in_junit(directory: Path, report_module) -> Path:
    """The process-lifecycle JUnit with every M3 stand-in process case passed."""
    root = ET.Element("testsuite", name="pytest")
    for test_id in report_module.M3_STAND_IN_TESTS:
        module_path, name = test_id.split("::")
        ET.SubElement(root, "testcase", name=name,
                      classname=module_path.removesuffix(".py").replace("/", "."))
    ET.ElementTree(root).write(directory / report_module.PINNED_PROCESS_LIFECYCLE,
                               encoding="utf-8", xml_declaration=True)
    return directory


def test_a_pinned_bundle_that_attests_the_tree_answers_m1_and_m3(report_module, tmp_path):
    evidence, rows = _pinned(report_module, _stand_in_junit(
        _bundle(tmp_path, report_module), report_module))

    assert evidence["problems"] == [], evidence["problems"]
    assert rows["M1"]["status"] == report_module.PASS
    assert rows["M3"]["status"] == report_module.PASS
    # The corrupt-download case is M3 evidence and is deliberately not counted in M1.
    assert rows["M1"]["measurements"]["matrix_passed"] == 2
    assert rows["M1"]["measurements"]["decoder"]["scene_order"] == ["scene-1", "scene-2"]
    assert rows["M3"]["measurements"]["submissions"]["first_submit"] == 1
    assert rows["M3"]["measurements"]["task_ids"]["first_submit"] == "task-1"
    assert rows["M3"]["measurements"]["decoder"]["bytes"] == 4096
    # The bundle records no process pair and no restart inside acceptance→staging, so
    # those two criteria stay open instead of inheriting the green parts.
    assert rows["M2"]["status"] == report_module.NOT_RUN
    assert rows["M4"]["status"] == report_module.NOT_RUN


def test_every_pinned_artefact_is_hashed_into_the_report(report_module, tmp_path):
    evidence, _ = _pinned(report_module, _bundle(tmp_path, report_module))

    assert set(evidence["artifacts"]) == {
        report_module.PINNED_SNAPSHOT,
        report_module.PINNED_RECOVERY,
        report_module.PINNED_MEDIA_MATRIX,
    }
    for name, artifact in evidence["artifacts"].items():
        assert len(artifact["sha256"]) == 64, name
        assert artifact["bytes"] > 0, name
    assert evidence["missing"] == []


def test_without_a_bundle_every_pinned_criterion_is_not_run(report_module):
    evidence, rows = _pinned(report_module, None)

    assert evidence["present"] is False
    assert all(row["status"] == report_module.NOT_RUN for row in rows.values())
    assert "no pinned evidence bundle" in evidence["problems"][0]


def test_a_green_bundle_from_another_sha_is_never_borrowed(report_module, tmp_path):
    foreign = {"vertep_sha": "1" * 40}
    _, rows = _pinned(report_module, _bundle(
        tmp_path, report_module,
        snapshot=_snapshot(report_module, **foreign),
        recovery=_recovery(report_module, sha="1" * 40),
    ))

    for criterion in ("M1", "M3", "M4"):
        assert rows[criterion]["status"] == report_module.FAIL
        assert "SHA" in rows[criterion]["detail"]


def test_a_configuration_revision_mismatch_is_not_evidence(report_module, tmp_path):
    _, rows = _pinned(report_module, _bundle(
        tmp_path, report_module,
        snapshot=_snapshot(report_module, config_revision="feedface"),
    ))

    assert rows["M1"]["status"] == report_module.FAIL
    assert "config revision" in rows["M1"]["detail"]


def test_an_upstream_pin_mismatch_is_not_evidence(report_module, tmp_path):
    _, rows = _pinned(report_module, _bundle(
        tmp_path, report_module,
        snapshot=_snapshot(report_module, runtime={
            "upstream_commit": "9" * 40,
            "bridge_schema_version": report_module.BRIDGE_SCHEMA_VERSION,
        }),
    ))

    assert rows["M1"]["status"] == report_module.FAIL
    assert "upstream" in rows["M1"]["detail"]


def test_two_artefacts_that_disagree_on_the_image_digest_are_not_evidence(
        report_module, tmp_path):
    _, rows = _pinned(report_module, _bundle(
        tmp_path, report_module,
        recovery=_recovery(report_module, image_digest="sha256:" + "b" * 64),
    ))

    assert rows["M1"]["status"] == report_module.FAIL
    assert "digest" in rows["M1"]["detail"]


def test_a_skipped_pinned_matrix_case_is_not_a_pass(report_module, tmp_path):
    voice = _matrix_case_name(report_module.VOICE_STAGING_NEGATIVE_TEST)
    _, rows = _pinned(report_module, _stand_in_junit(_bundle(
        tmp_path, report_module, matrix=_matrix(report_module, **{voice: "SKIPPED"}),
    ), report_module))

    assert rows["M1"]["status"] == report_module.NOT_RUN
    assert "skipped is not a pass" in rows["M1"]["detail"]
    # A stand-in skip in the media matrix does not contaminate recovery evidence.
    assert rows["M3"]["status"] == report_module.PASS


def test_the_corrupt_download_case_belongs_to_m3_not_m1(report_module, tmp_path):
    corrupt = _matrix_case_name(report_module.CORRUPT_DOWNLOAD_TEST)
    _, rows = _pinned(report_module, _bundle(
        tmp_path, report_module, matrix=_matrix(report_module, **{corrupt: "SKIPPED"}),
    ))

    assert rows["M1"]["status"] == report_module.PASS
    assert rows["M3"]["status"] == report_module.NOT_RUN
    assert "corrupt-download" in rows["M3"]["detail"]


def test_recovery_without_a_busy_delete_409_is_not_a_pass(report_module, tmp_path):
    _, rows = _pinned(report_module, _bundle(
        tmp_path, report_module,
        recovery=_recovery(report_module, assertions=[
            *report_module.M3_REQUIRED_ASSERTIONS,
            "the approved voice is still staged after the restart",
        ]),
    ))

    assert rows["M3"]["status"] == report_module.NOT_RUN
    assert "409" in rows["M3"]["detail"]


def test_a_failed_pinned_assertion_fails_the_criterion(report_module, tmp_path):
    first = report_module.M3_REQUIRED_ASSERTIONS[0]
    _, rows = _pinned(report_module, _bundle(
        tmp_path, report_module,
        recovery=_recovery(report_module, assertions=[
            {"check": first, "passed": False},
            *report_module.M3_REQUIRED_ASSERTIONS[1:],
            "a busy DELETE answers 409 instead of a false cancel",
        ]),
    ))

    assert rows["M3"]["status"] == report_module.FAIL
    assert first in rows["M3"]["detail"]


def test_a_missing_artefact_keeps_only_the_criteria_that_need_it_open(
        report_module, tmp_path):
    evidence, rows = _pinned(report_module, _bundle(
        tmp_path, report_module, omit=(report_module.PINNED_RECOVERY,),
    ))

    assert evidence["missing"] == [report_module.PINNED_RECOVERY]
    assert rows["M1"]["status"] == report_module.PASS
    assert rows["M3"]["status"] == report_module.NOT_RUN
    assert report_module.PINNED_RECOVERY in rows["M3"]["detail"]
    assert rows["M4"]["status"] == report_module.NOT_RUN


def test_a_restart_after_staging_does_not_stand_in_for_the_acceptance_window(
        report_module, tmp_path):
    _, rows = _pinned(report_module, _bundle(tmp_path, report_module))

    assert rows["M4"]["status"] == report_module.NOT_RUN
    assert "acceptance" in rows["M4"]["detail"]


def test_a_restart_between_acceptance_and_staging_answers_m4(report_module, tmp_path):
    _, rows = _pinned(report_module, _bundle(
        tmp_path, report_module,
        recovery=_recovery(report_module, assertions=[
            *report_module.M3_REQUIRED_ASSERTIONS,
            "a busy DELETE answers 409 instead of a false cancel",
            "a restart between acceptance and staging loses no approved input",
        ]),
    ))

    assert rows["M4"]["status"] == report_module.PASS


def test_the_markdown_report_shows_the_pinned_verdicts_and_hashes(report_module, tmp_path):
    evidence, rows = _pinned(report_module, _bundle(tmp_path, report_module))
    report = {
        "identity": _identity(report_module) | {
            "pinned_upstream": evidence["upstream"],
            "bridge_schema_version": evidence["bridge_schema_version"],
            "runtime_image_digest": evidence["image_digest"],
            "generated_at": "2026-11-01T00:00:00+00:00",
        },
        "rows": [],
        "pinned": evidence,
        "pinned_rows": list(rows.values()),
        "summary": {"pass": 0, "fail": 0, "not_run": 0, "declared_but_absent": [],
                    "pinned_pass": 2, "pinned_fail": 0, "pinned_not_run": 2},
    }

    rendered = report_module.render_markdown(report)

    assert "## Pinned evidence — M1–M4 (#389 / #392 M5)" in rendered
    assert "Pinned evidence — PASS: 2 · FAIL: 0 · NOT_RUN: 2" in rendered
    for name, artifact in evidence["artifacts"].items():
        assert f"`{name}` — sha256 `{artifact['sha256']}`" in rendered
    assert "attests this tree" in rendered


def _lifecycle_junit(directory: Path, report_module, outcome: str) -> None:
    root = ET.Element("testsuite", name="pytest")
    for test_id in report_module.PROCESS_LIFECYCLE_TESTS:
        module_path, name = test_id.split("::")
        case = ET.SubElement(root, "testcase", name=name,
                             classname=module_path.removesuffix(".py").replace("/", "."))
        if outcome == "FAILED":
            ET.SubElement(case, "failure", message="failed")
        elif outcome == "SKIPPED":
            ET.SubElement(case, "skipped", message="not run")
    ET.ElementTree(root).write(directory / report_module.PINNED_PROCESS_LIFECYCLE,
                               encoding="utf-8", xml_declaration=True)


@pytest.mark.parametrize("outcome,expected", [
    ("PASSED", "PASS"), ("SKIPPED", "NOT_RUN"), ("FAILED", "FAIL")])
def test_m2_is_answered_only_by_the_executed_process_lifecycle(report_module, tmp_path,
                                                                outcome, expected):
    """#397 M5: M2 is PASS only when both process tests ran on the pinned route."""
    directory = _bundle(tmp_path, report_module)
    _lifecycle_junit(directory, report_module, outcome)
    evidence, rows = _pinned(report_module, directory)
    assert rows["M2"]["status"] == getattr(report_module, expected)
    assert report_module.PINNED_PROCESS_LIFECYCLE in evidence["artifacts"]


def test_the_pinned_assertions_alone_are_not_full_m3_coverage(report_module, tmp_path):
    """#402 M5: a green 28-assertion bundle without the process cases is not M3 PASS."""
    _, rows = _pinned(report_module, _bundle(tmp_path, report_module))
    assert rows["M3"]["status"] == report_module.NOT_RUN
    assert "process cases were not executed" in rows["M3"]["detail"]
    cases = rows["M3"]["measurements"]["stand_in_process_cases"]
    assert all(case == {"expected": "PASSED", "route": "stand-in", "actual": "NOT_RUN",
                        "trace": {}} for case in cases.values())


def test_the_process_trace_is_carried_into_the_m3_row(report_module, tmp_path):
    """#402 M5: task IDs, submit counts and imported hashes recorded by the process case."""
    directory = _stand_in_junit(_bundle(tmp_path, report_module), report_module)
    path = directory / report_module.PINNED_PROCESS_LIFECYCLE
    tree = ET.parse(path)
    case = next(tree.getroot().iter("testcase"))
    properties = ET.SubElement(case, "properties")
    ET.SubElement(properties, "property", name="upstream_submits", value="1")
    ET.SubElement(properties, "property", name="upstream_task_ids", value='["task-1"]')
    tree.write(path, encoding="utf-8", xml_declaration=True)
    _, rows = _pinned(report_module, directory)
    trace = rows["M3"]["measurements"]["stand_in_process_cases"][
        report_module.M3_STAND_IN_TESTS[0]]["trace"]
    assert trace == {"upstream_submits": "1", "upstream_task_ids": '["task-1"]'}
