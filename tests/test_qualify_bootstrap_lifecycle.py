import importlib.util
from pathlib import Path
from subprocess import CompletedProcess


def module():
    path = Path(__file__).parents[1] / "scripts" / "qualify_bootstrap_lifecycle.py"
    spec = importlib.util.spec_from_file_location("bootstrap_lifecycle", path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(value)
    return value


def test_platform_probe_requires_expected_container_architecture(monkeypatch):
    target = module()
    monkeypatch.setattr(target, "command", lambda *args: CompletedProcess(args, 0, "aarch64\n", ""))
    assert target.docker_architecture("arm64") == "aarch64"


def test_platform_probe_refuses_wrong_container_architecture(monkeypatch):
    import pytest

    target = module()
    monkeypatch.setattr(target, "command", lambda *args: CompletedProcess(args, 0, "x86_64\n", ""))
    with pytest.raises(RuntimeError, match="expected 'aarch64'"):
        target.docker_architecture("arm64")


def test_junit_summary_keeps_skips_visible(tmp_path):
    target = module()
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuite><testcase name="pass"/><testcase name="skip"><skipped/></testcase>'
        '<testcase name="fail"><failure/></testcase><testcase name="error"><error/></testcase></testsuite>',
        encoding="utf-8",
    )
    assert target.junit_summary(report) == {"passed": 1, "failed": 1, "error": 1, "skipped": 1}


def test_executor_matrix_runs_inside_the_requested_architecture(tmp_path):
    target = module()
    argv = target.executor_command("arm64", tmp_path)
    assert argv[:5] == ["docker", "run", "--rm", "--platform", "linux/arm64"]
    script = argv[-1]
    # The recorded machine comes from the same container that runs the tests.
    assert "platform.machine()" in script and "/evidence/machine" in script
    assert all(name in script for name in target.EXECUTOR_TARGETS)


def _clean(passed=10):
    return {"passed": passed, "failed": 0, "error": 0, "skipped": 0}


def test_b2_passes_only_with_matching_executor_architecture_and_no_skips():
    target = module()
    host = {"returncode": 0, "pytest": _clean()}
    good = {"returncode": 0, "machine": "aarch64", "pytest": _clean()}
    assert target.criteria(good, host, "arm64")["B2"] == "PASS"
    assert target.criteria({**good, "machine": "x86_64"}, host, "arm64")["B2"] == "FAIL"
    assert target.criteria({**good, "pytest": {**_clean(), "skipped": 1}}, host, "arm64")["B2"] == "FAIL"
    assert target.criteria(good, {**host, "returncode": 1}, "arm64")["B2"] == "FAIL"
    assert target.criteria(good, host, "arm64")["B3"] == "NOT_RUN"
