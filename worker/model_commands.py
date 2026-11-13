"""Text-model catalog and CORE-issued model pull/delete commands of a Text Worker."""

import json
import os
import re
import socket
import threading
import time

import httpx

from worker.node_role import configured_role, node_capabilities


_MODEL_CATALOG_CACHE: dict = {"value": {}, "loaded": False, "checked_at": 0.0}
_MODEL_COMMANDS: dict[str, dict] = {}
_MODEL_COMMANDS_LOCK = threading.Lock()
_MODEL_COMMAND_LIMIT = 64


def text_model_catalog() -> dict:
    """Cached Ollama model catalog advertised to CORE in the heartbeat.

    Only text-capable nodes expose a catalog; the refresh interval keeps the
    heartbeat cheap while still letting CORE observe placement changes.
    """
    if configured_role() != "text" and "text_generation" not in node_capabilities():
        return {}
    interval = float(os.getenv("MODEL_CATALOG_INTERVAL_SECONDS", "30"))
    now = time.monotonic()
    if _MODEL_CATALOG_CACHE["loaded"] and now - _MODEL_CATALOG_CACHE["checked_at"] < interval:
        return _MODEL_CATALOG_CACHE["value"]
    try:
        response = httpx.get(f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/tags", timeout=15)
        response.raise_for_status()
        models = sorted({str(item.get("name")) for item in response.json().get("models", [])
                         if isinstance(item, dict) and item.get("name")})
        _MODEL_CATALOG_CACHE["value"] = {"models": models}
        _MODEL_CATALOG_CACHE["loaded"] = True
        _MODEL_CATALOG_CACHE["checked_at"] = now
    except (httpx.HTTPError, ValueError, AttributeError):
        if not _MODEL_CATALOG_CACHE["loaded"]:
            return {}
    return _MODEL_CATALOG_CACHE["value"]


def _report_model_progress(client: httpx.Client, core: str, node_name: str,
                           operation_id: str, status: str, phase: str,
                           progress: int, error: str | None = None) -> bool:
    """Send one progress sample to CORE; True means CORE wants us to stop."""
    try:
        response = client.post(f"{core}/api/workers/model-progress", json={
            "node_name": node_name, "operation_id": operation_id,
            "status": status, "phase": phase[:120],
            "progress": max(0, min(100, int(progress))),
            "error": str(error)[:2000] if error else None,
        })
        response.raise_for_status()
        return bool(response.json().get("cancel_requested"))
    except (httpx.HTTPError, ValueError, KeyError):
        return False


def _close_on_cancel(cancel_event: threading.Event, done_event: threading.Event,
                     response) -> None:
    """Abort the active pull transport the moment cancellation is signalled.

    The NDJSON reader only wakes when Ollama writes a line, so a stalled
    transport would otherwise keep the command thread blocked until the read
    timeout; shutting the socket down interrupts a reader blocked in ``recv``
    on every platform — a bare ``close()`` does not wake it on Linux — and
    closing the response then frees the client and command registry entry
    (Issue i.0.0.1.69).
    """
    while not done_event.is_set():
        if cancel_event.wait(0.05):
            try:
                network_stream = response.extensions.get("network_stream")
                sock = network_stream.get_extra_info("socket") if network_stream else None
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001 - the stream may already be gone
                pass
            try:
                response.close()
            except Exception:  # noqa: BLE001 - the transport is going away anyway
                pass
            return


def _run_model_command(core: str, node_name: str, command: dict,
                       client_options: dict, cancel_event: threading.Event) -> None:
    """Execute a node-local Ollama pull/delete and stream progress to CORE."""
    operation_id = str(command.get("operation_id") or "")
    action = str(command.get("action") or "")
    model = str(command.get("model") or "")
    base = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
    state = {"last_progress": 0, "last_sent": 0.0}

    def report(status: str, phase: str, progress: int,
               error: str | None = None, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and status == "RUNNING":
            if progress < state["last_progress"]:
                return False
            if now - state["last_sent"] < 1.0 and progress - state["last_progress"] < 2:
                return False
        stopped = _report_model_progress(client, core, node_name, operation_id,
                                         status, phase, progress, error)
        state["last_progress"] = max(state["last_progress"], progress)
        state["last_sent"] = now
        return stopped

    def stop_requested(status: str = "RUNNING", phase: str = "pull",
                       progress: int | None = None) -> bool:
        if cancel_event.is_set():
            report("CANCELLED", phase, state["last_progress"] if progress is None else progress,
                   "cancelled by CORE", force=True)
            return True
        if report(status, phase, state["last_progress"] if progress is None else progress):
            cancel_event.set()
            report("CANCELLED", phase, state["last_progress"] if progress is None else progress,
                   "cancelled by CORE", force=True)
            return True
        return False

    client: httpx.Client | None = None
    done_event = threading.Event()
    saw_success = False
    try:
        client = httpx.Client(**client_options)
        if action == "cancel":
            report("CANCELLED", "cancel", state["last_progress"], "cancelled by CORE", force=True)
            return
        if action == "delete":
            if stop_requested("RUNNING", "delete", 10):
                return
            response = client.request("DELETE", f"{base}/api/delete",
                                      json={"name": model}, timeout=120)
            if response.status_code >= 400:
                report("FAILED", "delete", 10,
                       f"Ollama returned {response.status_code}: {response.text[:300]}",
                       force=True)
                return
            report("COMPLETED", "delete", 100, force=True)
            return
        if action != "pull":
            report("FAILED", action or "unknown", 0,
                   f"Unsupported model command action: {action}", force=True)
            return

        if stop_requested("RUNNING", "pull", 0):
            return
        with client.stream("POST", f"{base}/api/pull",
                           json={"name": model, "stream": True},
                           timeout=httpx.Timeout(300.0, connect=10.0)) as response:
            if response.status_code != 200:
                body = response.read()[:500].decode("utf-8", "replace")
                report("FAILED", "pull", state["last_progress"],
                       f"Ollama returned {response.status_code}: {body}", force=True)
                return
            threading.Thread(target=_close_on_cancel,
                             args=(cancel_event, done_event, response),
                             daemon=True, name=f"pull-cancel-{operation_id[:8]}").start()
            for line in response.iter_lines():
                if cancel_event.is_set():
                    report("CANCELLED", "pull", state["last_progress"],
                           "cancelled by CORE", force=True)
                    return
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    report("FAILED", "pull", state["last_progress"],
                           str(event["error"])[:500], force=True)
                    return
                # The explicit Ollama ``success`` status is the only event that
                # proves the pull actually finished.
                if str(event.get("status") or "").lower() == "success":
                    saw_success = True
                completed, total = event.get("completed"), event.get("total")
                if completed is not None and total:
                    percent = max(0, min(100, int(completed * 100 // total)))
                    if stop_requested("RUNNING", "pull", percent):
                        return
                    state["last_progress"] = max(state["last_progress"], percent)
                elif event.get("status"):
                    if stop_requested("RUNNING", str(event["status"])[:120]):
                        return
        if cancel_event.is_set():
            # Cancellation raced the last line: report it instead of a success
            # the stream never actually produced.
            report("CANCELLED", "pull", state["last_progress"],
                   "cancelled by CORE", force=True)
            return
        if saw_success:
            report("COMPLETED", "pull", 100, force=True)
        else:
            # EOF without an explicit terminal success is a broken stream,
            # not a completed pull (Issue i.0.0.1.69).
            report("FAILED", "pull", state["last_progress"],
                   "pull ended without terminal success event", force=True)
    except Exception as error:  # noqa: BLE001 - a transport failure must surface durably
        if cancel_event.is_set():
            # The transport was closed on purpose by the cancellation watcher;
            # report the cancellation instead of a late failure.
            report("CANCELLED", "pull", state["last_progress"],
                   "cancelled by CORE", force=True)
        else:
            report("FAILED", action or "pull", state["last_progress"], str(error)[:500], force=True)
    finally:
        done_event.set()
        if client is not None:
            client.close()
        with _MODEL_COMMANDS_LOCK:
            entry = _MODEL_COMMANDS.get(operation_id)
            if entry is not None and entry["cancel"] is cancel_event:
                _MODEL_COMMANDS.pop(operation_id, None)


def handle_model_command(command: dict | None, core: str, node_name: str,
                         client_options: dict) -> None:
    """Start (or cancel) the thread that executes a heartbeat model command."""
    if not isinstance(command, dict):
        return
    operation_id = str(command.get("operation_id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        return
    action = str(command.get("action") or "")
    with _MODEL_COMMANDS_LOCK:
        entry = _MODEL_COMMANDS.get(operation_id)
        if entry is not None:
            if action == "cancel":
                entry["cancel"].set()
                if entry["thread"].is_alive():
                    return
            else:
                # Already executed once; never run the same placement twice.
                return
        if len(_MODEL_COMMANDS) > _MODEL_COMMAND_LIMIT:
            for stale_id, stale in list(_MODEL_COMMANDS.items()):
                if not stale["thread"].is_alive():
                    _MODEL_COMMANDS.pop(stale_id, None)
        cancel_event = threading.Event()
        if action == "cancel" and entry is not None:
            cancel_event = entry["cancel"]
        thread = threading.Thread(target=_run_model_command,
                                  args=(core, node_name, command, client_options, cancel_event),
                                  daemon=True, name=f"model-{operation_id[:8]}")
        _MODEL_COMMANDS[operation_id] = {"thread": thread, "cancel": cancel_event}
    thread.start()
