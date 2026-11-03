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


def test_green_not_proof_tests_never_prove_a_criterion(tmp_path):
    """#393: a not-proof test never makes a criterion PASS on its own; it stays visible
    with its reason. Only the criterion's proof tests decide PASS."""
    target = module()
    not_proof = [test for test in target.CRITERIA["F1"] if test in target.NOT_PROOF]
    assert not_proof
    alone = target.report([_junit(tmp_path / "alone.xml", {test: "PASS" for test in not_proof})])
    assert alone["criteria"]["F1"]["overall"] == "PARTIAL"

    every = {test: "PASS" for tests in target.CRITERIA.values() for test in tests}
    full = target.report([_junit(tmp_path / "all.xml", every)])["criteria"]["F1"]
    assert full["overall"] == "PASS"
    rows = {row["test_id"]: row for row in full["tests"]}
    assert all(rows[test]["proof"] is False and rows[test]["not_proof_reason"] for test in not_proof)
    assert all(row["expected"] == "PASS" and row["junit"] for row in full["tests"])


def test_open_source_subcases_keep_a_green_criterion_partial(tmp_path, monkeypatch):
    """#401 P5: one passing test is not the whole criterion (an open subcase keeps it PARTIAL)."""
    target = module()
    every = {test: "PASS" for tests in target.CRITERIA.values() for test in tests}
    junit = _junit(tmp_path / "all.xml", every)
    monkeypatch.setitem(target.OPEN_SUBCASES, "P1", ["an unexecuted subcase"])
    assert target.report([junit])["criteria"]["P1"]["overall"] == "PARTIAL"
    monkeypatch.setitem(target.OPEN_SUBCASES, "P1", [])
    assert target.report([junit])["criteria"]["P1"]["overall"] == "PASS"


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
