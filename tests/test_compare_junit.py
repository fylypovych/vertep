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
