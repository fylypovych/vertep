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


def test_a_lost_sigterm_is_repeated_before_the_delayed_side_effect(tmp_path):
    """CI arm64 (qemu-user): a SIGTERM that reaches a grandchild between fork and
    exec can be lost. The watchdog keeps signalling the group while it waits, so
    such a grandchild still dies before its delayed side effect (3s), long before
    the SIGKILL deadline."""
    agent = _load_agent()
    marker = tmp_path / "late-side-effect.txt"
    grandchild = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"   # the first SIGTERM is lost
        "time.sleep(0.5)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
        "time.sleep(2.5)\n"
        "open(sys.argv[1], 'w').write('applied')\n")
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time\n"
         "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
         "time.sleep(30)\n", grandchild, str(marker)],
        start_new_session=True)
    try:
        time.sleep(0.3)  # the grandchild is ignoring SIGTERM right now
        started = time.monotonic()
        agent._terminate_process(parent)
        assert time.monotonic() - started < 3.0, "the group must stop before the side effect"
        time.sleep(3.5)
        assert not marker.exists(), "a grandchild that lost SIGTERM survived the fence"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)


def test_zombies_do_not_count_as_alive(tmp_path):
    agent = _load_agent()
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    group = os.getpgid(child.pid)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and agent._get_group_pids(group):
        time.sleep(0.05)
    assert agent._get_group_pids(group) == []  # exited but not yet reaped: a zombie
    child.wait(timeout=10)
