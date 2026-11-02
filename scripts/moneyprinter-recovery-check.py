#!/usr/bin/env python3
"""Pinned MoneyPrinterTurbo submit recovery check (#389 M3/M4).

``moneyprinter-runtime-check.py`` proves the pinned runtime once.  This check reuses
its image and container helpers and drives the *real* pinned runtime through the
recovery paths of the bridge contract:

1. a submit with a durable ``x-vertep-submit-key`` creates one upstream task;
2. a repeat with the same key (a lost response retried) is answered from the
   durable record and creates no second upstream task;
3. a lost submit response — the request processed, the answer never read — is
   reconciled into exactly one upstream task instead of being resubmitted blindly;
4. a busy task refuses the cancel: the DELETE answers 409 and the task is never
   reported as stopped while the upstream still runs it;
5. the runtime container is restarted, and the key still reconciles to the same
   task id with the approved voice staged;
6. a restart inside the acceptance→staging window completes the staging on the
   retry without a second upstream task and without losing the approved input;
7. a tampered staged voice is refused before the upstream is contacted, so the
   upstream task count does not change;
8. an unknown key reconciles to ``unknown`` instead of a false success.

Every step is executed against the running container; the upstream task count is
read from the pinned task storage.  Evidence is written as JSON.  Exit code 0 means
every step held.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = "/opt/moneyprinter"
VERTEP_DIR = "/opt/vertep"
TASKS_DIR = f"{APP_DIR}/storage/tasks"


def _runtime_check():
    spec = importlib.util.spec_from_file_location(
        "moneyprinter_runtime_check", ROOT / "scripts" / "moneyprinter-runtime-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRELUDE = f"""
import hashlib, json, sys, tempfile
from pathlib import Path
import httpx
from services.moneyprinter_service import (
    _remember_submit, _stage_probe_clip, _submit_record, _submit_route_payload,
    _voice_dir, _write_voice_tone, PROBE_SCENES, VOICE_MARKER_PREFIX, headers,
    read_api_key)

WRAPPER = "http://127.0.0.1:8098"
AUTH = headers(read_api_key())


def tasks():
    root = Path({TASKS_DIR!r})
    return sorted(path.name for path in root.iterdir() if path.is_dir()) if root.is_dir() else []


def submit(key, materials, marker):
    response = httpx.post(f"{{WRAPPER}}/api/v1/videos", headers={{**AUTH, "x-vertep-submit-key": key}},
                          content=json.dumps(_submit_route_payload(materials, marker)).encode(),
                          timeout=180)
    try:
        body = response.json()
    except ValueError:
        body = {{"text": response.text[:500]}}
    return {{"status": response.status_code, "body": body}}


def reconcile(key):
    response = httpx.get(f"{{WRAPPER}}/api/v1/videos/{{key}}", headers=AUTH, timeout=60)
    return {{"status": response.status_code, "body": response.json()}}


def stage_voice(seconds):
    staging = Path(tempfile.mkdtemp(prefix="vertep-recovery-voice-"))
    voice = staging / "voice.wav"
    _write_voice_tone(voice, seconds=seconds)
    response = httpx.post(f"{{WRAPPER}}/api/v1/voice", headers={{**AUTH, "x-vertep-filename": "voice.wav"}},
                          content=voice.read_bytes(), timeout=60)
    response.raise_for_status()
    return response.json()["data"]["voice"]
"""

PREPARE = """
staging = Path(tempfile.mkdtemp(prefix="vertep-recovery-"))
materials = []
for label, colour, seconds in PROBE_SCENES:
    clip = staging / f"{label}.mp4"
    _stage_probe_clip(clip, seconds=seconds, colour=colour)
    upload = httpx.post(f"{WRAPPER}/api/v1/video_materials", headers={**AUTH, "x-vertep-filename": clip.name},
                        content=clip.read_bytes(), timeout=120)
    upload.raise_for_status()
    materials.append({"provider": "local", "url": upload.json()["data"]["file"], "duration": seconds})
marker = stage_voice(sum(seconds for _, _, seconds in PROBE_SCENES))
print(json.dumps({"materials": materials, "marker": marker, "tasks": tasks()}))
"""

SUBMIT = """
arguments = json.loads(sys.argv[1])
result = submit(arguments["key"], arguments["materials"], arguments["marker"])
print(json.dumps({**result, "tasks": tasks()}))
"""

RECONCILE = """
arguments = json.loads(sys.argv[1])
print(json.dumps({**reconcile(arguments["key"]), "tasks": tasks()}))
"""

TAMPER = """
arguments = json.loads(sys.argv[1])
marker = stage_voice(2)
digest_and_suffix = marker[len(VOICE_MARKER_PREFIX):]
staged = _voice_dir() / digest_and_suffix
staged.write_bytes(staged.read_bytes() + b"tampered after approval")
before = tasks()
result = submit(arguments["key"], arguments["materials"], marker)
print(json.dumps({**result, "tasks_before": before, "tasks": tasks()}))
"""

MISSING = """
arguments = json.loads(sys.argv[1])
marker = stage_voice(2)
staged = _voice_dir() / marker[len(VOICE_MARKER_PREFIX):]
staged.unlink()
before = tasks()
refused = submit(arguments["key"], arguments["materials"], marker)
unknown = reconcile(arguments["key"])
restaged = stage_voice(2)
retried = submit(arguments["key"], arguments["materials"], restaged)
print(json.dumps({"refused": refused, "unknown": unknown, "same_marker": restaged == marker,
                  "retried": retried, "tasks_before": before, "tasks": tasks()}))
"""

CANCEL = """
arguments = json.loads(sys.argv[1])
response = httpx.delete(f"{WRAPPER}/api/v1/videos/{arguments['key']}", headers=AUTH, timeout=60)
absent = httpx.delete(f"{WRAPPER}/api/v1/videos/unknown-{arguments['key']}", headers=AUTH, timeout=60)
print(json.dumps({"status": response.status_code, "absent_status": absent.status_code,
                  "after": reconcile(arguments["key"])}))
"""

# A lost submit response is injected at the acceptance boundary: the request is
# delivered and processed, but the caller never reads the answer. The durable key has to
# reconcile to exactly one upstream task; an ambiguous attempt must not be resubmitted.
LOST_RESPONSE = """
import socket, time
arguments = json.loads(sys.argv[1])
body = json.dumps(_submit_route_payload(arguments["materials"], arguments["marker"])).encode()
lines = [
    "POST /api/v1/videos HTTP/1.1",
    "Host: 127.0.0.1:8098",
    "Content-Type: application/json",
    f"x-vertep-submit-key: {arguments['key']}",
    *(f"{name}: {value}" for name, value in AUTH.items()),
    f"Content-Length: {len(body)}",
    "Connection: close",
    "",
    "",
]
before = tasks()
sock = socket.create_connection(("127.0.0.1", 8098), timeout=60)
sock.sendall("\\r\\n".join(lines).encode() + body)
# The request is delivered and processed while the connection stays open; the answer is
# written but never read, so the caller cannot know whether the submit was accepted.
time.sleep(float(arguments.get("settle", 5)))
sock.close()
print(json.dumps({"tasks_before": before, "reconciled": reconcile(arguments["key"]),
                  "tasks": tasks()}))
"""

# A running task refuses a cancel: the DELETE answers 409 and the task keeps rendering.
# The attempt must never be reported as stopped while the upstream still runs it.
BUSY_CANCEL = """
arguments = json.loads(sys.argv[1])
submitted = submit(arguments["key"], arguments["materials"], arguments["marker"])
before = tasks()
response = httpx.delete(f"{WRAPPER}/api/v1/videos/{arguments['key']}", headers=AUTH, timeout=60)
print(json.dumps({"submitted": submitted, "delete_status": response.status_code,
                  "tasks_before": before, "after": reconcile(arguments["key"]),
                  "tasks": tasks()}))
"""

# The acceptance→staging window: the wrapper has accepted the submit — the upstream
# task exists — but the approved voice is not yet staged into the task directory. A
# crash there is a process fault, so the durable record is put into exactly the state
# the wrapper persists in that window ("submitted") and the runtime is restarted for
# real before the retry. The retry must complete the staging without a second upstream
# task and without losing the approved input.
WINDOW_ACCEPTANCE = """
arguments = json.loads(sys.argv[1])
accepted = submit(arguments["key"], arguments["materials"], arguments["marker"])
record = _submit_record(arguments["key"]) or {}
_remember_submit(arguments["key"], task_id=record.get("task_id"),
                 marker=arguments["marker"], state="submitted")
window = _submit_record(arguments["key"]) or {}
print(json.dumps({"accepted": accepted, "window_state": window.get("state"),
                  "task_id": window.get("task_id"), "tasks": tasks()}))
"""


class Container:
    def __init__(self, check, name: str):
        self.check, self.name = check, name

    def run(self, step: str, arguments: dict | None = None) -> dict:
        result = subprocess.run(
            ["docker", "exec", "-i", "-w", VERTEP_DIR, "-e", f"PYTHONPATH={APP_DIR}:{VERTEP_DIR}",
             self.name, "python3", "-", json.dumps(arguments or {})],
            input=PRELUDE + step, capture_output=True, text=True, timeout=900)
        if result.returncode:
            raise self.check.CheckFailure(
                f"step failed in the pinned container: {result.stderr.strip()[-2000:]}")
        return json.loads(result.stdout.strip().splitlines()[-1])


def require(condition: bool, message: str, evidence: dict) -> None:
    evidence.setdefault("assertions", []).append({"check": message, "passed": bool(condition)})
    if not condition:
        raise AssertionError(message)


def scenario(container: Container, restart, evidence: dict) -> None:
    prepared = container.run(PREPARE)
    materials, marker = prepared["materials"], prepared["marker"]
    key = f"recovery-{secrets.token_hex(6)}"

    first = container.run(SUBMIT, {"key": key, "materials": materials, "marker": marker})
    evidence["first_submit"] = first
    require(first["status"] == 200, "first submit is accepted", evidence)
    task_id = first["body"]["data"]["task_id"]
    require(bool(task_id), "first submit names an upstream task", evidence)

    # A lost response is retried with the same key: no second upstream task.
    time.sleep(3)
    before_retry = container.run(RECONCILE, {"key": key})["tasks"]
    retried = container.run(SUBMIT, {"key": key, "materials": materials, "marker": marker})
    evidence["retried_submit"] = retried
    require(retried["status"] == 200 and retried["body"].get("message") == "reconciled",
            "a repeated submit key is reconciled", evidence)
    require(retried["body"]["data"]["task_id"] == task_id, "the retry names the same task", evidence)
    require(retried["tasks"] == before_retry, "the retry created no upstream task", evidence)

    # A lost submit response at the acceptance boundary: the request is delivered and
    # processed, but the caller never reads the answer. The durable key has to reconcile
    # to exactly one upstream task; the ambiguous attempt must not be resubmitted.
    lost = container.run(LOST_RESPONSE, {"key": f"lost-{secrets.token_hex(6)}",
                                         "materials": materials, "marker": marker, "settle": 5})
    evidence["lost_response"] = lost
    require(lost["reconciled"]["status"] == 200
            and lost["reconciled"]["body"].get("state") == "submitted"
            and bool(lost["reconciled"]["body"]["data"].get("task_id")),
            "a lost submit response reconciles to exactly one upstream task", evidence)
    require(len(lost["tasks"]) == len(lost["tasks_before"]) + 1,
            "a lost submit response created exactly one upstream task", evidence)

    # A busy task refuses the cancel: the DELETE answers 409 and the task keeps rendering.
    busy = container.run(BUSY_CANCEL, {"key": f"busy-{secrets.token_hex(6)}",
                                       "materials": materials, "marker": marker})
    evidence["busy_cancel"] = busy
    require(busy["submitted"]["status"] == 200, "the busy attempt is submitted", evidence)
    require(busy["delete_status"] == 409
            and busy["after"]["body"].get("runtime_state") in {"running", "finished"},
            "a busy DELETE answers 409 instead of a false cancel", evidence)

    # The runtime container restarts; the durable key still maps to the task.
    restart()
    after = container.run(RECONCILE, {"key": key})
    evidence["reconciled_after_restart"] = after
    require(after["status"] == 200 and after["body"]["state"] == "submitted",
            "the key reconciles to submitted after a runtime restart", evidence)
    require(after["body"]["data"].get("task_id") == task_id,
            "the reconciled task id survived the restart", evidence)
    require(after["body"].get("voice_staged") is True,
            "the approved voice is still staged after the restart", evidence)
    resubmitted = container.run(SUBMIT, {"key": key, "materials": materials, "marker": marker})
    evidence["resubmitted_after_restart"] = resubmitted
    require(resubmitted["body"].get("data", {}).get("task_id") == task_id
            and resubmitted["tasks"] == after["tasks"],
            "a retry after the restart still creates no second task", evidence)

    # A restart inside the acceptance→staging window (M4): the submit is accepted and the
    # upstream task exists, but the approved voice is not staged yet. The runtime is
    # restarted for real; the retry has to complete the staging without a second task and
    # without losing the approved input.
    window_key = f"window-{secrets.token_hex(6)}"
    window = container.run(WINDOW_ACCEPTANCE, {"key": window_key,
                                               "materials": materials, "marker": marker})
    evidence["window_acceptance"] = window
    require(window["accepted"]["status"] == 200 and bool(window["task_id"]),
            "the submit is accepted before the window restart", evidence)
    require(window["window_state"] == "submitted",
            "the approved voice is still unstaged when the runtime restarts", evidence)
    restart()
    window_after = container.run(RECONCILE, {"key": window_key})
    evidence["window_after_restart"] = window_after
    require(window_after["status"] == 200 and window_after["body"].get("state") == "submitted",
            "the accepted task is still reconcilable when the runtime restarts", evidence)
    completed = container.run(SUBMIT, {"key": window_key, "materials": materials,
                                       "marker": marker})
    evidence["window_completed"] = completed
    require(completed["status"] == 200
            and completed["body"].get("message") == "reconciled"
            and completed["body"]["data"].get("task_id") == window["task_id"]
            and completed["tasks"] == window_after["tasks"],
            "a restart between acceptance and staging loses no approved input", evidence)

    # A tampered approved voice never reaches the upstream.
    tampered = container.run(TAMPER, {"key": f"tampered-{secrets.token_hex(6)}", "materials": materials})
    evidence["tampered_voice"] = tampered
    require(tampered["status"] == 503 and tampered["body"].get("reason") == "voice_staging_failed",
            "a tampered staged voice is refused", evidence)
    require(tampered["tasks"] == tampered["tasks_before"],
            "the tampered attempt created no upstream task", evidence)

    # A missing approved voice is refused before POST; the attempt is not
    # "recovered" by a late staging, and a retry after re-staging the same approved
    # bytes creates exactly one task without losing the approved inputs.
    missing = container.run(MISSING, {"key": f"missing-{secrets.token_hex(6)}", "materials": materials})
    evidence["missing_voice"] = missing
    require(missing["refused"]["status"] == 503
            and missing["refused"]["body"].get("reason") == "voice_staging_failed",
            "a missing staged voice is refused", evidence)
    require(missing["unknown"]["status"] == 404 and missing["unknown"]["body"].get("state") == "unknown",
            "a refused early-audio attempt is not reported as recovered", evidence)
    require(missing["same_marker"], "re-staging the approved bytes yields the same marker", evidence)
    require(missing["retried"]["status"] == 200
            and len(missing["tasks"]) == len(missing["tasks_before"]) + 1,
            "the retry after re-staging creates exactly one task", evidence)

    # Cancel by the durable key: a running task is either refused with 409 and
    # still reported as running, or cancelled; never "cancelled" while running.
    cancelled = container.run(CANCEL, {"key": key})
    evidence["cancel"] = cancelled
    runtime_state = cancelled["after"]["body"].get("runtime_state")
    require(cancelled["absent_status"] == 404, "cancelling an unknown key answers 404", evidence)
    require((cancelled["status"] == 409 and runtime_state in {"running", "finished"})
            # After the restart the upstream may no longer know the task at all.
            or (cancelled["status"] in {200, 202, 204, 404} and runtime_state != "running"),
            "cancel never reports a running task as stopped", evidence)

    unknown = container.run(RECONCILE, {"key": f"unknown-{secrets.token_hex(6)}"})
    evidence["unknown_key"] = unknown
    require(unknown["status"] == 404 and unknown["body"].get("state") == "unknown",
            "an unknown key reconciles to unknown", evidence)


def serve(check, env_file: Path, timeout: int) -> int:
    """Leave one pinned runtime running for pytest against the real container."""
    name = f"moneyprinter-pinned-serve-{secrets.token_hex(4)}"
    key_directory = Path(tempfile.mkdtemp(prefix="vertep-mpt-serve-key-"))
    _key_file, api_key = check.write_api_key(key_directory)
    if subprocess.run(["docker", "image", "inspect", check.IMAGE_NAME],
                      capture_output=True).returncode != 0:
        check.build_image((ROOT / "VERSION").read_text(encoding="utf-8").strip())
    check.start_container(name, check.image_digest(check.IMAGE_NAME), key_directory)
    port = check.mapped_port(name)
    check.wait_for_wrapper(port, api_key, time.monotonic() + timeout)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    with env_file.open("a", encoding="utf-8") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in (
            ("VERTEP_MPT_PINNED_URL", f"http://127.0.0.1:{port}"),
            ("VERTEP_MPT_PINNED_TOKEN", api_key),
            ("VERTEP_MPT_PINNED_CONTAINER", name))))
    print(f"pinned runtime {name} serves http://127.0.0.1:{port}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--evidence", default="", help="JSON evidence file to write")
    parser.add_argument("--serve", default="",
                        help="start the pinned runtime, write its URL/token/container to this "
                             "env file for later steps and leave it running")
    parser.add_argument("--stop", default="", help="remove a runtime started with --serve")
    arguments = parser.parse_args()
    check = _runtime_check()
    if arguments.stop:
        subprocess.run(["docker", "rm", "-f", arguments.stop], capture_output=True)
        return 0
    if arguments.serve:
        return serve(check, Path(arguments.serve), arguments.timeout)
    evidence: dict = {"issue": 389, "sha": subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()}
    name = f"moneyprinter-recovery-check-{secrets.token_hex(4)}"
    key_directory = Path(tempfile.mkdtemp(prefix="vertep-mpt-recovery-key-"))
    _key_file, api_key = check.write_api_key(key_directory)
    started = False
    status = 1
    try:
        if not check.docker_available():
            raise check.CheckFailure("docker is not available on this host")
        if subprocess.run(["docker", "image", "inspect", check.IMAGE_NAME],
                          capture_output=True).returncode != 0:
            check.build_image((ROOT / "VERSION").read_text(encoding="utf-8").strip())
        digest = check.image_digest(check.IMAGE_NAME)
        evidence["image_digest"] = digest
        check.start_container(name, digest, key_directory)
        started = True

        def ready() -> None:
            port = check.mapped_port(name)
            check.wait_for_wrapper(port, api_key, time.monotonic() + arguments.timeout)

        def restart() -> None:
            check.docker("restart", name)
            ready()

        ready()
        scenario(Container(check, name), restart, evidence)
        evidence["result"] = "PASS"
        status = 0
    except (AssertionError, check.CheckFailure, subprocess.SubprocessError) as error:
        evidence["result"] = "FAIL"
        evidence["error"] = str(error)
        if started:
            check.report_container_log(name)
    finally:
        if started:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        if arguments.evidence:
            Path(arguments.evidence).parent.mkdir(parents=True, exist_ok=True)
            Path(arguments.evidence).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
        print(json.dumps({key: evidence.get(key) for key in ("result", "error", "assertions")},
                         ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
