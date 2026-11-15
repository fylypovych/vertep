"""The fenced-update watchdog signals only the spawned process group.

procps-ng 4.0.4 ``/usr/bin/kill -SIG -PGID`` delivered the signal to every
process of the user, so stopping a cancelled update killed unrelated processes
(on a host running the agent as root: the whole host). The agent now uses only
``os.killpg`` on the dedicated child group.
"""

import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")


def _load_agent():
    spec = importlib.util.spec_from_file_location("update_agent_scope", ROOT / "scripts" / "update-agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)


def test_only_the_target_group_is_signalled():
    agent = _load_agent()
    target, bystander = _sleeper(), _sleeper()
    try:
        agent._kill_process_group(os.getpgid(target.pid), signal.SIGTERM)
        target.wait(timeout=10)
        time.sleep(0.2)
        assert target.returncode == -signal.SIGTERM
        assert bystander.poll() is None, "a process of another group must not be signalled"
    finally:
        for process in (target, bystander):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_terminate_process_stops_the_tree_and_spares_others():
    agent = _load_agent()
    tree = subprocess.Popen([sys.executable, "-c",
                             "import subprocess, sys, time\n"
                             "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                             "time.sleep(30)\n"], start_new_session=True)
    bystander = _sleeper()
    try:
        time.sleep(0.5)
        group = os.getpgid(tree.pid)
        agent._terminate_process(tree)
        assert tree.poll() is not None
        assert agent._get_group_pids(group) == []
        assert bystander.poll() is None
    finally:
        for process in (tree, bystander):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


@pytest.mark.parametrize("pgid", [0, 1, -5, "own"])
def test_shared_or_invalid_groups_are_refused(monkeypatch, pgid):
    agent = _load_agent()
    sent = []
    monkeypatch.setattr(agent.os, "killpg", lambda group, sig: sent.append((group, sig)))
    agent._kill_process_group(os.getpgid(0) if pgid == "own" else pgid, signal.SIGKILL)
    assert sent == []


def test_no_external_kill_binaries_are_used():
    source = (ROOT / "scripts" / "update-agent.py").read_text(encoding="utf-8")
    assert '["kill"' not in source and '["pkill"' not in source
