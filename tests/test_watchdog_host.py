import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("override", [False, True])
def test_host_watchdog_reads_status_without_core_package(tmp_path, override):
    script = tmp_path / "scripts" / "watchdog.py"
    script.parent.mkdir()
    script.write_bytes((Path(__file__).parents[1] / "scripts/watchdog.py").read_bytes())
    state = tmp_path / ("custom-update" if override else "config/update")
    state.mkdir(parents=True)
    status = state / "status.json"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("UPDATE_STATE_DIR", None)
    env["VERTEP_ROOT"] = str(tmp_path)
    if override:
        env["UPDATE_STATE_DIR"] = str(state)
    code = ("import runpy; "
            "assert runpy.run_path('scripts/watchdog.py')['check_update_agent']() "
            "== {'update_agent': %s}")
    for value, expected in [("SUCCEEDED", True), ("FAILED", False)]:
        status.write_text(json.dumps({"state": value}))
        result = subprocess.run([sys.executable, "-I", "-c", code % expected],
                                cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
