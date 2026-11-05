"""`vertep update` follows the update to its outcome instead of only queueing it."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH, reason="bash is required")


def _status(state, progress=50, phase="UPDATING", message="m", **extra):
    return {"request_id": "r1", "state": state, "progress": progress, "phase": phase, "message": message,
            "current_version": "1.0", "available_version": "1.1", "update_available": True, **extra}


def _run(tmp_path, statuses, post_code="200", env=None):
    root = tmp_path / "install"
    (root / "scripts").mkdir(parents=True)
    shutil.copy("scripts/vertep", root / "scripts" / "vertep")
    (root / "scripts" / "vertep").write_bytes((root / "scripts" / "vertep").read_bytes().replace(b"\r\n", b"\n"))
    script = tmp_path / "bin"
    script.mkdir()
    (tmp_path / "statuses.json").write_text(json.dumps(statuses), encoding="utf-8")
    (script / "fake_curl.py").write_text(f"""
import json, sys
post = "POST" in sys.argv
if post:
    output = sys.argv[sys.argv.index("-o") + 1]
    open(output, "w").write(json.dumps({{"request_id": "r1", "state": "PENDING"}}))
    sys.stdout.write({post_code!r})
    sys.exit(0)
path = {str(tmp_path / "statuses.json")!r}
items = json.load(open(path))
item = items.pop(0) if len(items) > 1 else items[0]
json.dump(items, open(path, "w"))
if item is None:
    sys.exit(7)
sys.stdout.write(json.dumps(item))
""", encoding="utf-8")
    python = Path(sys.executable).as_posix()
    for name, body in (("curl", f'exec "{python}" "{(script / "fake_curl.py").as_posix()}" "$@"\n'),
                       ("python3", f'exec "{python}" "$@"\n')):
        (script / name).write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
        (script / name).chmod((script / name).stat().st_mode | stat.S_IEXEC)
    return subprocess.run([BASH, "scripts/vertep", "update"], cwd=root, capture_output=True, encoding="utf-8",
                          timeout=60, env={**os.environ, "PATH": f"{script}{os.pathsep}{os.environ['PATH']}",
                                           "PYTHONUTF8": "1", "VERTEP_UPDATE_POLL_SECONDS": "0", **(env or {})})


def test_follows_progress_through_a_core_restart_to_success(tmp_path):
    done = _status("SUCCEEDED", 100, "NORMAL", "Update completed")
    result = _run(tmp_path, [_status("RUNNING", 18, "DOWNLOADING"), None, done])
    assert result.returncode == 0, result.stderr
    assert "[18%] DOWNLOADING" in result.stdout and "CORE перезапускається" in result.stdout
    assert "Оновлення завершено: версія 1.1" in result.stdout


def test_says_when_there_is_nothing_to_update(tmp_path):
    result = _run(tmp_path, [_status("SUCCEEDED", 100, "NORMAL", update_available=False)])
    assert result.returncode == 0 and "Оновлень немає" in result.stdout


@pytest.mark.parametrize("state", ["FAILED", "ROLLED_BACK"])
def test_failure_is_reported_with_a_failing_exit_code(tmp_path, state):
    result = _run(tmp_path, [_status(state, message="причина")])
    assert result.returncode == 1 and "причина" in result.stderr


def test_request_the_agent_never_picks_up_is_reported(tmp_path):
    result = _run(tmp_path, [_status("PENDING", 1, "CHECKING")], env={"VERTEP_UPDATE_PICKUP_SECONDS": "1"})
    assert result.returncode == 1 and "Агент оновлень не взяв запит" in result.stderr
