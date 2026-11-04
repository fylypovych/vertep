"""#393 P5: the criterion report reads JUnit and never infers PASS."""

import importlib.util
import json
import xml.etree.ElementTree as ET
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


def test_source_403_links_each_remainder_to_its_tests_route_and_runtime(tmp_path):
    """#403 R4: R3 is PASS only around the real runtime; a green stand-in stays PARTIAL."""
    target = module()
    tests = [test for spec in target.SOURCE_403.values() for test in spec["tests"]]

    def write(route: str) -> Path:
        root = ET.Element("testsuite", name="pytest")
        for test_id in tests:
            module_path, name = test_id.split("::")
            case = ET.SubElement(root, "testcase", name=name,
                                 classname=module_path.removesuffix(".py").replace("/", "."))
            properties = ET.SubElement(case, "properties")
            ET.SubElement(properties, "property", name="route", value=route)
        directory = tmp_path / route
        directory.mkdir()
        (directory / "runtime-snapshot.json").write_text(json.dumps(
            {"image_digest": "sha256:abc", "pinned_upstream": "u@1", "config_revision": "c",
             "vertep_sha": "s"}), encoding="utf-8")
        path = directory / "junit.xml"
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
        return path

    proxied = target.report([write("pinned-proxy")])
    assert {row["overall"] for row in proxied["source_403"].values()} == {"PASS"}
    assert proxied["pinned_runtime"]["image_digest"] == "sha256:abc"
    row = proxied["source_403"]["R3"]["tests"][0]
    assert row["expected"] == "PASS" and row["route"] == "pinned-proxy" and row["junit"]
    assert proxied["source_403"]["R3"]["source"] == "#402 M3"

    stand_in = target.report([write("stand-in")])["source_403"]
    assert stand_in["R3"]["overall"] == "PARTIAL" and stand_in["R1"]["overall"] == "PASS"


def test_source_403_r3_is_the_whole_m3_fault_matrix():
    """The two reports must not drift apart: the R3 matrix is the M3 process list."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "issue_122_report", SCRIPT.parent / "issue-122-acceptance-report.py")
    report_122 = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = report_122
    spec.loader.exec_module(report_122)
    assert set(module().SOURCE_403["R3"]["tests"]) == set(report_122.M3_STAND_IN_TESTS)
