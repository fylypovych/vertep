"""`curl … | bash` must run bootstrap to the end even when a step reads stdin.

Piped into bash, the script itself is stdin. Without one enclosing brace group, a step
such as ``docker compose exec`` during the backup swallowed the rest of the script and
bootstrap stopped after "Backing up the installed system" with exit code 0.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def test_bootstrap_is_parsed_whole_before_anything_runs():
    lines = (ROOT / "bootstrap.sh").read_text(encoding="utf-8").splitlines()
    statements = [line for line in lines if line.strip() and not line.startswith("#")]
    assert statements[0] == "{" and statements[-1] == "}"
    subprocess.run(["bash", "-n", str(ROOT / "bootstrap.sh")], check=True)


def test_backup_survives_live_file_changes_but_not_tar_failure():
    script = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
    assert "|| tar_status=$?" in script
    assert "(( tar_status <= 1 )) || fail" in script


def test_archive_live_tolerates_changed_files_but_not_failures(tmp_path):
    source = (ROOT / "scripts" / "vertep").read_text(encoding="utf-8")
    helper = source[source.index("archive_live(){"):source.index("sync_host_runtime(){")]
    script = helper + (
        'archive_live "$1/changed.gz" bash -c "echo data | gzip; exit 1" && echo changed-ok\n'
        'archive_live "$1/stopped.gz" bash -c "exit 1" || echo stopped-fails\n'
        'archive_live "$1/fatal.gz" bash -c "echo data | gzip; exit 2" || echo fatal-fails\n'
    )
    posix = tmp_path.as_posix()
    if len(posix) > 1 and posix[1] == ":":  # Git Bash on Windows
        posix = f"/{posix[0].lower()}{posix[2:]}"
    out = subprocess.run(["bash", "-c", script, "bash", posix], capture_output=True,
                         text=True, timeout=30).stdout.split()
    assert out == ["changed-ok", "stopped-fails", "fatal-fails"]


def test_runtime_bundle_keeps_cli_executable():
    builder = (ROOT / "scripts" / "build-runtime-bundle.py").read_text(encoding="utf-8")
    assert "shutil.copy(source, destination)" in builder


def test_a_step_reading_stdin_does_not_swallow_a_piped_script():
    piped = "{\nset -e\necho before\ncat >/dev/null\necho after\nexit 0\n}\n"
    unwrapped = "set -e\necho before\ncat >/dev/null\necho after\n"
    run = lambda script: subprocess.run(["bash"], input=script, capture_output=True,
                                        text=True, timeout=30).stdout.split()
    assert run(piped) == ["before", "after"]
    assert run(unwrapped) == ["before"], "the failure mode this guards against"
