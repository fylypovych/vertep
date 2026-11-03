"""#393 P5: the criterion report reads JUnit and never infers PASS."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "report_criterion_390.py"


def module():
    spec = importlib.util.spec_from_file_location("report_criterion_390", SCRIPT)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def _junit(path, results):
    cases = []
    for test_id, state in results.items():
        module_path, name = test_id.split("::")
        body = {"PASS": "", "FAIL": "<failure/>", "SKIPPED": "<skipped/>"}[state]
        classname = module_path.removesuffix(".py").replace("/", ".")
        cases.append(f'<testcase classname="{classname}" name="{name}">{body}</testcase>')
    path.write_text("<testsuite>" + "".join(cases) + "</testsuite>", encoding="utf-8")
    return path


def test_without_junit_every_criterion_is_not_run():
    report = module().report([])
    assert {value["overall"] for value in report["criteria"].values()} == {"NOT_RUN"}


def test_green_not_proof_tests_cap_the_criterion_at_partial(tmp_path):
    target = module()
    every = {test: "PASS" for tests in target.CRITERIA.values() for test in tests}
    criteria = target.report([_junit(tmp_path / "all.xml", every)])["criteria"]
    assert criteria["F1"]["overall"] == "PARTIAL"
    assert criteria["P4"]["overall"] == "PARTIAL"


def test_open_source_subcases_keep_a_green_criterion_partial(tmp_path, monkeypatch):
    """#401 P5: one passing test is not the whole criterion (F3 terminal-success only)."""
    target = module()
    every = {test: "PASS" for tests in target.CRITERIA.values() for test in tests}
    junit = _junit(tmp_path / "all.xml", every)
    assert target.report([junit])["criteria"]["F3"]["overall"] == "PARTIAL"
    monkeypatch.setitem(target.OPEN_SUBCASES, "F3", [])
    assert target.report([junit])["criteria"]["F3"]["overall"] == "PASS"


def test_skipped_is_not_run_and_failed_is_fail(tmp_path):
    target = module()
    telegram = target.CRITERIA["F3"][0]
    skipped = target.report([_junit(tmp_path / "s.xml", {telegram: "SKIPPED"})])["criteria"]
    assert skipped["F3"]["overall"] == "NOT_RUN"
    failed = target.report([_junit(tmp_path / "f.xml", {telegram: "FAIL"})])["criteria"]
    assert failed["F3"]["overall"] == "FAIL"


def test_junit_of_another_sha_is_never_counted(tmp_path):
    """#396 P5: evidence of a different commit is NOT_RUN, not PASS."""
    target = module()
    every = {test: "PASS" for tests in target.CRITERIA.values() for test in tests}
    junit = _junit(tmp_path / "all.xml", every)
    value = target.report([junit], run_sha="0" * 40)
    assert {row["overall"] for row in value["criteria"].values()} == {"NOT_RUN"}
    assert "error" in value and value["junit_sha256"][str(junit)]
