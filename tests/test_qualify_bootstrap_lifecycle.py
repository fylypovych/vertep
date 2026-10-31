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
