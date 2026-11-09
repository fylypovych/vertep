import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("compare_junit", Path(__file__).parents[1] / "scripts" / "compare_junit.py")
compare_junit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare_junit)


def test_compare_junit_detects_outcome_difference(tmp_path):
    a, b = tmp_path / "a.xml", tmp_path / "b.xml"
    a.write_text('<testsuite><testcase classname="t" name="x"/><testcase classname="t" name="y"/></testsuite>')
    b.write_text('<testsuite><testcase classname="t" name="x"/><testcase classname="t" name="y"><failure/></testcase></testsuite>')
    assert compare_junit.diff(compare_junit.outcomes(a), compare_junit.outcomes(a)) == []
    assert compare_junit.diff(compare_junit.outcomes(a), compare_junit.outcomes(b)) == ["t::y: passed != failure"]


def test_compare_junit_ignores_xdist_group_suffix_and_module_skips(tmp_path):
    a, b = tmp_path / "a.xml", tmp_path / "b.xml"
    a.write_text('<testsuite><testcase classname="t" name="x@together"/></testsuite>')
    b.write_text('<testsuite><testcase classname="t" name="x"/><testcase classname="" name="tests.m"><skipped/></testcase></testsuite>')
    assert compare_junit.diff(compare_junit.outcomes(a), compare_junit.outcomes(b)) == []


def test_compare_junit_reports_totals_and_raw_module_skips(tmp_path, capsys):
    a, b = tmp_path / "a.xml", tmp_path / "b.xml"
    a.write_text('<testsuite time="2"><testcase classname="t" name="x@g"/></testsuite>')
    b.write_text('<testsuite time="5"><testcase classname="t" name="x"/>'
                 '<testcase classname="" name="tests.m"><skipped/></testcase></testsuite>')
    assert compare_junit.main([str(a), str(b), str(a)]) == 0
    out = capsys.readouterr().out
    assert "whole-module skip (raw record, not counted as a case): tests.m" in out
    assert "'passed': 1" in out and "'suite_time_s': 5.0" in out
    assert out.count("Outcomes identical") == 2
