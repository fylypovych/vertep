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


def test_a_step_reading_stdin_does_not_swallow_a_piped_script():
    piped = "{\nset -e\necho before\ncat >/dev/null\necho after\nexit 0\n}\n"
    unwrapped = "set -e\necho before\ncat >/dev/null\necho after\n"
    run = lambda script: subprocess.run(["bash"], input=script, capture_output=True,
                                        text=True, timeout=30).stdout.split()
    assert run(piped) == ["before", "after"]
    assert run(unwrapped) == ["before"], "the failure mode this guards against"
