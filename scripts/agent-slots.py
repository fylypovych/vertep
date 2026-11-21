#!/usr/bin/env python3
"""Autonomous A/B recovery of the Update Agent (#514, VAR #509).

Two verified local slots hold the agent (``scripts/update-agent.py`` and the ``core``
package it imports): ``<root>/agent/slots/a`` and ``.../b``; ``<root>/agent/active``
names the active one. Bootstrap installs a new agent into the inactive slot and
switches; the switch is accepted only when the new slot passes its local readiness
check, otherwise the previous slot stays active. The Watchdog runs ``check``: a slot
that fails its own integrity or readiness check is replaced by the previous verified
slot. Everything is local: neither CORE nor its package cache is needed, and CORE
being unreachable is never a reason to reject a working slot.

  install --root R --source DIR --version V   copy into the inactive slot and verify it
  switch  --root R                            activate the inactive slot if it is ready
  check   --root R                            Watchdog: keep a ready slot active
  run     --root R -- AGENT-ARGS              run the agent from the active slot
  status  --root R
Stdlib only: it runs with the host's python3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SLOTS = ("a", "b")
PAYLOAD = ("scripts/update-agent.py", "core")
READY_TIMEOUT = float(os.getenv("VERTEP_AGENT_READY_TIMEOUT", "60"))


def _base(root: Path) -> Path:
    return root / "agent"


def _atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def state(root: Path) -> dict:
    try:
        return json.loads((_base(root) / "active.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": None, "previous": None, "events": []}


def _save_state(root: Path, value: dict, event: str) -> dict:
    value.setdefault("events", []).append({"at": time.time(), "event": event})
    value["events"] = value["events"][-50:]
    _atomic(_base(root) / "active.json", value)
    return value


def _files(folder: Path) -> dict[str, str]:
    result = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and path.name != "slot.json":
            result[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def verify(root: Path, slot: str) -> dict:
    """Integrity of a slot: every file matches its recorded hash, nothing missing or added."""
    folder = _base(root) / "slots" / slot
    try:
        meta = json.loads((folder / "slot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "reason": "slot is not installed"}
    if _files(folder) != meta.get("files"):
        return {"ok": False, "reason": "slot files do not match their hashes", "version": meta.get("version")}
    return {"ok": True, "version": meta.get("version")}


def ready(root: Path, slot: str) -> dict:
    """Local readiness only: integrity + the agent's own self-test. CORE is not contacted."""
    integrity = verify(root, slot)
    if not integrity["ok"]:
        return integrity
    folder = _base(root) / "slots" / slot
    try:
        result = subprocess.run([sys.executable, "-I", str(folder / "scripts" / "update-agent.py"), "--self-test",
                                 "--root", str(root), "--state-dir", str(root / "config" / "update")],
                                capture_output=True, text=True, timeout=READY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "self-test timed out", "version": integrity["version"]}
    if result.returncode != 0:
        return {"ok": False, "reason": (result.stderr or result.stdout).strip()[-500:] or "self-test failed",
                "version": integrity["version"]}
    return {"ok": True, "version": integrity["version"]}


def install(root: Path, source: Path, version: str) -> dict:
    current = state(root)
    target = next(slot for slot in SLOTS if slot != current.get("active"))
    folder = _base(root) / "slots" / target
    staging = folder.with_name(f".{target}.staging")
    shutil.rmtree(staging, ignore_errors=True)
    for item in PAYLOAD:
        origin = source / item
        if origin.is_dir():
            shutil.copytree(origin, staging / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif origin.is_file():
            (staging / item).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, staging / item)
        else:
            shutil.rmtree(staging, ignore_errors=True)
            raise SystemExit(f"Agent payload is missing {item}")
    _atomic(staging / "slot.json", {"version": version, "installed_at": time.time(), "files": _files(staging)})
    shutil.rmtree(folder, ignore_errors=True)
    staging.replace(folder)
    result = verify(root, target)
    if not result["ok"]:
        raise SystemExit(f"Installed slot {target} failed verification: {result['reason']}")
    _save_state(root, {**current, "staged": target}, f"installed {version} into slot {target}")
    return {"slot": target, "version": version}


def switch(root: Path) -> dict:
    current = state(root)
    target = current.get("staged") or next(slot for slot in SLOTS if slot != current.get("active"))
    check = ready(root, target)
    if not check["ok"]:
        _save_state(root, {**current, "staged": None}, f"slot {target} not ready, kept {current.get('active')}: {check['reason']}")
        return {"switched": False, "active": current.get("active"), "reason": check["reason"]}
    previous = current.get("active")
    _save_state(root, {**current, "active": target, "previous": previous, "staged": None},
                f"switched to slot {target} ({check['version']})")
    return {"switched": True, "active": target, "previous": previous, "version": check["version"]}


def watchdog_check(root: Path) -> dict:
    """Keep a ready slot active; a broken new slot gives way to the previous verified one."""
    current = state(root)
    active = current.get("active")
    if active is None:
        return {"ok": True, "active": None, "reason": "agent slots are not initialized"}
    check = ready(root, active)
    if check["ok"]:
        return {"ok": True, "active": active, "version": check["version"]}
    previous = current.get("previous") or next(slot for slot in SLOTS if slot != active)
    fallback = ready(root, previous)
    if not fallback["ok"]:
        _save_state(root, current, f"slot {active} failed ({check['reason']}); no ready slot to return to")
        return {"ok": False, "active": active, "reason": check["reason"]}
    _save_state(root, {**current, "active": previous, "previous": active},
                f"slot {active} failed ({check['reason']}); returned to slot {previous}")
    return {"ok": True, "active": previous, "reverted_from": active, "reason": check["reason"]}


def run(root: Path, agent_args: list[str]) -> int:
    """Run the agent from the active slot; without slots, from the runtime and seed slot a."""
    current = state(root)
    active = current.get("active")
    if active is None:
        if (root / "scripts" / "update-agent.py").is_file() and (root / "core").is_dir():
            version = (root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").exists() else "unknown"
            install(root, root, version)
            switch(root)
            active = state(root).get("active")
    script = (_base(root) / "slots" / active / "scripts" / "update-agent.py") if active else root / "scripts" / "update-agent.py"
    # The slot holds only the agent code; the version and .env are those of the installation.
    code = subprocess.call([sys.executable, str(script), *agent_args], env={**os.environ, "VERTEP_ROOT": str(root)})
    # A runtime update brought a new agent: stage it in the other slot; it becomes active
    # only after its own readiness check, otherwise the working slot stays.
    runtime_version = (root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").exists() else None
    active_version = verify(root, state(root).get("active") or "a").get("version")
    if runtime_version and active_version and runtime_version != active_version:
        install(root, root, runtime_version)
        switch(root)
    return code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "switch", "check", "run", "status"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name == "install":
            command.add_argument("--source", type=Path, required=True)
            command.add_argument("--version", required=True)
        if name == "run":
            command.add_argument("agent_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "run":
        agent_args = [item for item in args.agent_args if item != "--"]
        raise SystemExit(run(root, agent_args))
    result = {"install": lambda: install(root, args.source.resolve(), args.version), "switch": lambda: switch(root),
              "check": lambda: watchdog_check(root),
              "status": lambda: {**state(root), "slots": {slot: verify(root, slot) for slot in SLOTS}}}[args.command]()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "check" and not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
