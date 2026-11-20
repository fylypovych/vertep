"""#514: autonomous A/B recovery of the Update Agent — local, without CORE."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SLOTS = ROOT / "scripts" / "agent-slots.py"
SYSTEM_PYTHON = shutil.which("python3", path="/usr/bin:/bin") or sys.executable


def runtime(tmp_path: Path, version: str) -> Path:
    """A host runtime as the bundle lays it out: scripts, core, trust key, VERSION."""
    root = tmp_path / "opt-vertep"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("update-agent.py", "agent-slots.py", "vertep"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    if (root / "core").exists():
        shutil.rmtree(root / "core")
    shutil.copytree(ROOT / "core", root / "core", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "installer").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "installer" / "update-public.pem", root / "installer" / "update-public.pem")
    (root / "config" / "update").mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(version + "\n")
    return root


def slots(root: Path, *args: str, check: bool = True) -> dict:
    # No CORE anywhere: an unreachable address, no network needed by any step.
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "CORE_ADDRESS": "http://127.0.0.1:9",
           "VERTEP_AGENT_READY_TIMEOUT": "60"}
    result = subprocess.run([SYSTEM_PYTHON, "-I", str(root / "scripts" / "agent-slots.py"), *args, "--root", str(root)],
                            capture_output=True, text=True, timeout=180, env=env)
    if check:
        assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip().startswith("{") else {"rc": result.returncode}


def break_import(root: Path, slot: str) -> None:
    """A new agent that installs cleanly but cannot start (its own code fails)."""
    folder = root / "agent" / "slots" / slot
    target = folder / "core" / "update_trust.py"
    target.write_text("raise RuntimeError('broken agent build')\n")
    meta = json.loads((folder / "slot.json").read_text())
    import hashlib
    meta["files"]["core/update_trust.py"] = hashlib.sha256(target.read_bytes()).hexdigest()
    (folder / "slot.json").write_text(json.dumps(meta))


def test_switch_activates_a_ready_slot_and_keeps_the_previous_one(tmp_path):
    root = runtime(tmp_path, "0.0.4.20")
    assert slots(root, "install", "--source", str(root), "--version", "0.0.4.20")["slot"] == "a"
    assert slots(root, "switch") == {"switched": True, "active": "a", "previous": None, "version": "0.0.4.20"}
    assert slots(root, "install", "--source", str(root), "--version", "0.0.4.21")["slot"] == "b"
    switched = slots(root, "switch")
    assert switched["active"] == "b" and switched["previous"] == "a"
    status = slots(root, "status")
    assert status["slots"]["a"]["ok"] and status["slots"]["b"]["ok"]


@pytest.mark.parametrize("fault", ["integrity", "self-test"])
def test_a_new_slot_that_is_not_ready_never_becomes_active(tmp_path, fault):
    root = runtime(tmp_path, "0.0.4.20")
    slots(root, "install", "--source", str(root), "--version", "0.0.4.20")
    slots(root, "switch")
    slots(root, "install", "--source", str(root), "--version", "0.0.4.21")
    if fault == "integrity":
        (root / "agent" / "slots" / "b" / "scripts" / "update-agent.py").write_text("# tampered\n")
    else:
        break_import(root, "b")
    result = slots(root, "switch")
    assert result["switched"] is False and result["active"] == "a"
    assert slots(root, "check")["active"] == "a"


def test_the_watchdog_returns_to_the_previous_slot_without_core(tmp_path):
    root = runtime(tmp_path, "0.0.4.20")
    slots(root, "install", "--source", str(root), "--version", "0.0.4.20")
    slots(root, "switch")
    slots(root, "install", "--source", str(root), "--version", "0.0.4.21")
    slots(root, "switch")
    break_import(root, "b")  # the active new agent fails later on this node
    result = slots(root, "check")
    assert result == {**result, "ok": True, "active": "a", "reverted_from": "b"}
    assert "broken agent build" in result["reason"]
    events = [e["event"] for e in json.loads((root / "agent" / "active.json").read_text())["events"]]
    assert any("returned to slot a" in event for event in events)


def test_an_unreachable_core_alone_never_rejects_a_working_slot(tmp_path):
    root = runtime(tmp_path, "0.0.4.20")
    slots(root, "install", "--source", str(root), "--version", "0.0.4.20")
    slots(root, "switch")
    for _ in range(2):
        assert slots(root, "check") == {"ok": True, "active": "a", "version": "0.0.4.20"}


def test_no_ready_slot_is_reported_and_nothing_switches(tmp_path):
    root = runtime(tmp_path, "0.0.4.20")
    slots(root, "install", "--source", str(root), "--version", "0.0.4.20")
    slots(root, "switch")
    break_import(root, "a")
    env = {"PATH": os.environ["PATH"], "HOME": str(root)}
    result = subprocess.run([SYSTEM_PYTHON, "-I", str(root / "scripts" / "agent-slots.py"), "check", "--root", str(root)],
                            capture_output=True, text=True, timeout=180, env=env)
    report = json.loads(result.stdout)
    assert result.returncode == 1 and report["ok"] is False and report["active"] == "a"
    assert json.loads((root / "agent" / "active.json").read_text())["active"] == "a"


def test_run_seeds_the_slots_and_stages_the_agent_of_a_new_runtime(tmp_path):
    root = runtime(tmp_path, "0.0.4.20")
    env = {"PATH": os.environ["PATH"], "HOME": str(root)}
    agent = ["--", "--self-test", "--root", str(root), "--state-dir", str(root / "config" / "update")]
    run = subprocess.run([SYSTEM_PYTHON, "-I", str(root / "scripts" / "agent-slots.py"), "run", "--root", str(root), *agent],
                         capture_output=True, text=True, timeout=180, env=env)
    assert run.returncode == 0, run.stderr
    assert slots(root, "status")["active"] == "a"
    runtime(tmp_path, "0.0.4.21")  # the runtime was updated: its agent is the next one
    run = subprocess.run([SYSTEM_PYTHON, "-I", str(root / "scripts" / "agent-slots.py"), "run", "--root", str(root), *agent],
                         capture_output=True, text=True, timeout=180, env=env)
    assert run.returncode == 0, run.stderr
    status = slots(root, "status")
    assert status["active"] == "b" and status["previous"] == "a" and status["slots"]["b"]["version"] == "0.0.4.21"


def test_the_update_unit_runs_the_agent_from_its_slot():
    unit = (ROOT / "installer" / "vertep-update.service").read_text()
    assert "scripts/agent-slots.py run --root @VERTEP_ROOT@ -- --root @VERTEP_ROOT@" in unit
