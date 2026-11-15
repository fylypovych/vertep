"""bootstrap.sh first step: install Vertep or the Vertep laboratory; update what is installed.

The selection functions are executed from the real bootstrap.sh in bash, with a fake
``curl`` and in a new session (no controlling terminal, as in CI and automation).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX bash")


def _functions() -> str:
    start = BOOTSTRAP.index("run_lab_bootstrap(){")
    end = BOOTSTRAP.index("\nselect_install_target\n")
    return BOOTSTRAP[start:end]


def _run(tmp_path, *, vertep=False, lab=False, target=None, lab_ref=None):
    install_root, lab_root = tmp_path / "opt-vertep", tmp_path / "opt-lab"
    install_root.mkdir()
    lab_root.mkdir()
    if vertep:
        (install_root / ".env").write_text("VERTEP_VERSION=0.0.3.77\n")
    if lab:
        (lab_root / "lab.env").write_text("LAB_OWNER_TOKEN=x\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "curl-calls.txt"
    # Fake curl: the release API answers a tag; the raw URL "downloads" a stand-in
    # bootstrap-lab.sh that reports how it was started.
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{calls}"\n'
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        'case "$*" in\n'
        '  *releases/latest*) echo \'{"tag_name": "0.0.3.77"}\';;\n'
        '  *bootstrap-lab.sh*) printf \'echo "LAB-BOOTSTRAP ref=$VERTEP_LAB_REF"\\n\' > "$out";;\n'
        "esac\n")
    (bin_dir / "curl").chmod(0o755)
    for tool in ("sed", "head", "mktemp", "bash", "cat"):
        if shutil.which(tool):
            (bin_dir / tool).symlink_to(shutil.which(tool))
    script = (
        'fail(){ printf "VERTEP: %s\\n" "$*" >&2; exit 1; }\n'
        'progress(){ printf "==> %s\\n" "$*"; }\n'
        + _functions()
        + "\nselect_install_target\necho CONTINUE-VERTEP\n")
    env = {"PATH": str(bin_dir), "VERTEP_LAB_ROOT": str(lab_root)}
    if target is not None:
        env["VERTEP_INSTALL_TARGET"] = target
    if lab_ref is not None:
        env["VERTEP_LAB_REF"] = lab_ref
    result = subprocess.run([shutil.which("bash"), "-c", f'INSTALL_ROOT="{install_root}"\n' + script],
                            capture_output=True, text=True, timeout=30, env=env,
                            start_new_session=True, stdin=subprocess.DEVNULL)
    return result, (calls.read_text() if calls.exists() else "")


def test_installed_vertep_is_updated_without_a_question(tmp_path):
    result, calls = _run(tmp_path, vertep=True)
    assert result.returncode == 0 and "CONTINUE-VERTEP" in result.stdout
    assert calls == "" and "Що встановити" not in result.stdout


def test_installed_lab_is_updated_by_its_own_bootstrap_at_the_release_tag(tmp_path):
    result, calls = _run(tmp_path, lab=True)
    assert result.returncode == 0, result.stderr
    assert "LAB-BOOTSTRAP ref=0.0.3.77" in result.stdout
    assert "CONTINUE-VERTEP" not in result.stdout  # the Vertep flow never runs
    assert "/0.0.3.77/bootstrap-lab.sh" in calls


def test_first_install_without_a_terminal_keeps_installing_vertep(tmp_path):
    result, calls = _run(tmp_path)
    assert result.returncode == 0 and "CONTINUE-VERTEP" in result.stdout
    assert calls == ""


def test_first_install_can_choose_the_lab_for_automation(tmp_path):
    result, _ = _run(tmp_path, target="lab", lab_ref="0.0.3.72")
    assert "LAB-BOOTSTRAP ref=0.0.3.72" in result.stdout
    assert "CONTINUE-VERTEP" not in result.stdout


def test_first_install_can_choose_vertep_explicitly(tmp_path):
    result, _ = _run(tmp_path, target="vertep")
    assert "CONTINUE-VERTEP" in result.stdout


@pytest.mark.parametrize("vertep,lab,target,message", [
    (True, False, "lab", "install the laboratory on a separate VM"),
    (False, True, "vertep", "install Vertep on a separate VM"),
    (False, False, "both", "must be 'vertep' or 'lab'"),
])
def test_conflicts_and_invalid_targets_fail_without_changes(tmp_path, vertep, lab, target, message):
    result, calls = _run(tmp_path, vertep=vertep, lab=lab, target=target)
    assert result.returncode == 1 and message in result.stderr
    assert "CONTINUE-VERTEP" not in result.stdout and calls == ""


def test_the_selection_is_the_first_step_after_the_root_check():
    root_check = BOOTSTRAP.index('|| fail "run bootstrap as root')
    selection = BOOTSTRAP.index("\nselect_install_target\n")
    assert root_check < selection
    for later in ('service_manager=${VERTEP_SERVICE_MANAGER:-auto}', 'progress "Detecting hardware"',
                  "existing_runtime=false", "release_probe=$(curl"):
        assert selection < BOOTSTRAP.index(later), later


def test_the_question_is_read_from_the_terminal_not_the_piped_script():
    functions = _functions()
    assert re.search(r"exec 3</dev/tty", functions)
    assert 'read -r -p "Ваш вибір [1-2, типово 1]: " answer <&3' in functions


def _interactive(tmp_path, answer: str) -> str:
    """Run the selection with a real controlling terminal and type ``answer``."""
    import pty
    import time

    lab_root = tmp_path / "opt-lab"
    install_root = tmp_path / "opt-vertep"
    lab_root.mkdir()
    install_root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        'case "$*" in\n'
        '  *releases/latest*) echo \'{"tag_name": "0.0.3.77"}\';;\n'
        '  *bootstrap-lab.sh*) printf \'echo "LAB-BOOTSTRAP ref=$VERTEP_LAB_REF"\\n\' > "$out";;\n'
        "esac\n")
    (bin_dir / "curl").chmod(0o755)
    for tool in ("sed", "head", "mktemp", "bash", "cat"):
        if shutil.which(tool):
            (bin_dir / tool).symlink_to(shutil.which(tool))
    script = (f'INSTALL_ROOT="{install_root}"\n'
              'fail(){ printf "VERTEP: %s\\n" "$*" >&2; exit 1; }\n'
              'progress(){ printf "==> %s\\n" "$*"; }\n'
              + _functions() + "\nselect_install_target\necho CONTINUE-VERTEP\n")
    pid, fd = pty.fork()
    if pid == 0:  # child: the pty is its controlling terminal (/dev/tty)
        os.execve(shutil.which("bash"), ["bash", "-c", script],
                  {"PATH": str(bin_dir), "VERTEP_LAB_ROOT": str(lab_root)})
    output = b""
    deadline = time.monotonic() + 20
    sent = False
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        if not sent and "Ваш вибір".encode() in output:
            os.write(fd, answer.encode() + b"\n")
            sent = True
    os.waitpid(pid, 0)
    return output.decode("utf-8", "replace")


@pytest.mark.parametrize("answer,expected", [("2", "LAB-BOOTSTRAP ref=0.0.3.77"),
                                             ("", "CONTINUE-VERTEP"),
                                             ("1", "CONTINUE-VERTEP")])
def test_first_install_asks_on_the_terminal(tmp_path, answer, expected):
    output = _interactive(tmp_path, answer)
    assert "Що встановити на цей сервер?" in output
    assert expected in output
