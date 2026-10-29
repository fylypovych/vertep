"""Disposable CORE process runner for the Telegram acceptance (Issue #253).

The runner starts a *real* CORE process (or a slice of it) against a
temporary durable state directory and a controlled Telegram transport, so the
acceptance can observe crash, replay and restart behaviour of the production
code instead of a simulation of it.

Modes:
    fixtures
        Create the acceptance Job, channel and Publisher Worker in the
        disposable state directory and exit.
    callback <callback_id> <chat_id> <data>
        Run one Telegram callback through the production dispatcher.
    crash-after-claim <callback_id> <chat_id> <data>
        Run one callback and kill the process inside the handler, right after
        the durable claim was persisted and before the effect runs.
    serve <port> [--crash-after-ack <node_name>]
        Run the production CORE API (uvicorn) with the Telegram polling
        service enabled.  With ``--crash-after-ack`` the process dies as soon
        as the node acknowledged the restart, so the effect survives the
        process death.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.telegram.handlers as _owner_core_telegram_handlers


def _callback(callback_id: str, chat_id: str, data: str) -> dict:
    return {
        "id": callback_id,
        "from": {"id": int(chat_id), "is_bot": False, "first_name": "operator"},
        "chat_instance": "controlled",
        "data": data,
        "message": {"message_id": 1, "chat": {"id": int(chat_id), "type": "private"},
                    "text": "controlled"},
    }


def _install_fixtures() -> None:
    from core.state import store
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from telegram_acceptance_fixtures import install

    print(json.dumps(install(store), ensure_ascii=False))


def _watch_for_restart_ack(node_name: str) -> None:
    """Kill the process once the node acknowledged the restart."""
    workers_file = Path(os.getenv("JOB_ROOT", "jobs")) / ".workers.json"

    def _watch() -> None:
        while True:
            try:
                workers = json.loads(workers_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                workers = {}
            worker = workers.get(node_name) or {}
            if worker.get("restart_ack"):
                os._exit(9)
            time.sleep(0.005)

    threading.Thread(target=_watch, name="restart-ack-watch", daemon=True).start()


def main() -> None:
    mode = sys.argv[1]
    if mode == "fixtures":
        _install_fixtures()
        return
    if mode in {"callback", "crash-after-claim"}:
        callback_id, chat_id, data = sys.argv[2], sys.argv[3], sys.argv[4]
        import core.app as app

        if mode == "crash-after-claim":
            def _crash(*_args, **_kwargs):
                # The durable claim is already on disk; the effect never runs.
                os._exit(9)

            from core.telegram import handlers
            handlers._handle_system_callback = _crash
        result = _owner_core_telegram_handlers._handle_telegram_callback(_callback(callback_id, chat_id, data))
        payload = {"ok": True}
        if isinstance(result, dict):
            payload["result"] = result
        print(json.dumps(payload, ensure_ascii=False))
        return
    if mode == "serve":
        import uvicorn

        if "--crash-after-ack" in sys.argv:
            _watch_for_restart_ack(sys.argv[sys.argv.index("--crash-after-ack") + 1])
        uvicorn.run("core.app:app", host="127.0.0.1", port=int(sys.argv[2]),
                    log_level="warning", access_log=False)
        return
    raise SystemExit(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
