"""Controlled Telegram Bot API transport for the automated Telegram acceptance.

The transport is a real HTTP server that speaks the subset of the Telegram
Bot API used by ``adapters/telegram.py``.  It lets the acceptance run the
production adapter, the production polling service and the production CORE
against a disposable endpoint instead of the public Telegram API, so callback
delivery, crash/replay and restart behaviour can be observed end to end
(Issue #253).

Only the transport is faked: the orchestration, the durable state and the
publisher path stay the production ones.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class ControlledTelegramTransport:
    """Disposable Telegram Bot API endpoint with recorded calls."""

    def __init__(self, token: str = "controlled-token") -> None:
        self.token = token
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._updates: list[dict[str, Any]] = []
        self._next_update_id = 1
        self._calls: list[dict[str, Any]] = []
        self._files: dict[str, bytes] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="controlled-telegram", daemon=True)
        self._thread.start()

    # -- lifecycle ---------------------------------------------------------
    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "ControlledTelegramTransport":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- test API ----------------------------------------------------------
    def push_update(self, update: dict[str, Any]) -> int:
        """Queue an update for the next ``getUpdates`` call."""
        with self._condition:
            update = dict(update)
            update.setdefault("update_id", self._next_update_id)
            self._next_update_id = int(update["update_id"]) + 1
            self._updates.append(update)
            self._condition.notify_all()
            return int(update["update_id"])

    def push_callback_query(self, callback_id: str, chat_id: str, data: str,
                            user_id: str = "42") -> int:
        """Queue a ``callback_query`` update for the given chat."""
        with self._lock:
            update_id = self._next_update_id
            self._next_update_id += 1
        return self.push_update({
            "update_id": update_id,
            "callback_query": {
                "id": callback_id,
                "from": {"id": int(user_id), "is_bot": False, "first_name": "operator"},
                "chat_instance": "controlled",
                "data": data,
                "message": {"message_id": 1, "chat": {"id": int(chat_id), "type": "private"},
                            "text": "controlled"},
            },
        })

    def calls(self, method: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if method is None:
                return list(self._calls)
            return [call for call in self._calls if call["method"] == method]

    def messages(self) -> list[dict[str, Any]]:
        return self.calls("sendMessage")

    def answers(self) -> list[dict[str, Any]]:
        return self.calls("answerCallbackQuery")

    def answer_texts(self) -> list[str]:
        return [str((call.get("payload") or {}).get("text") or "") for call in self.answers()]

    def message_texts(self) -> list[str]:
        return [str((call.get("payload") or {}).get("text") or "") for call in self.messages()]

    def answers_for(self, callback_id: str) -> list[dict[str, Any]]:
        """Recorded answers of one callback query."""
        with self._lock:
            return [call for call in self._calls
                    if call["method"] == "answerCallbackQuery"
                    and (call.get("payload") or {}).get("callback_query_id") == callback_id]

    def messages_for(self, chat_id: str) -> list[dict[str, Any]]:
        """Recorded messages sent to one chat."""
        with self._lock:
            return [call for call in self._calls
                    if call["method"] == "sendMessage"
                    and str((call.get("payload") or {}).get("chat_id")) == str(chat_id)]

    def add_file(self, path: str, content: bytes) -> None:
        with self._lock:
            self._files[path] = content

    # -- internals ---------------------------------------------------------
    def _collect_updates(self, offset: int, timeout: float) -> list[dict[str, Any]]:
        """Return queued updates, waiting like the real long-polling API."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                updates = [update for update in self._updates
                           if int(update["update_id"]) >= offset]
                if updates:
                    return updates
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(timeout=min(0.2, remaining))

    # -- server ------------------------------------------------------------
    def _handler(self):
        transport = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                return

            def _parts(self) -> list[str]:
                return [part for part in urlparse(self.path).path.split("/") if part]

            def _payload(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                raw = self.rfile.read(length)
                content_type = self.headers.get("Content-Type") or ""
                if "application/json" in content_type:
                    try:
                        parsed = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        return {}
                    return parsed if isinstance(parsed, dict) else {}
                return {"_raw_length": len(raw)}

            def _respond(self, body: dict[str, Any]) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parts = self._parts()
                if parts and parts[0] == "file":
                    name = parts[-1]
                    with transport._lock:
                        content = transport._files.get(name)
                    if content is None:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return

                method = parts[-1] if parts else ""
                if method == "getUpdates":
                    query = parse_qs(urlparse(self.path).query)
                    try:
                        offset = int((query.get("offset") or ["0"])[0])
                    except ValueError:
                        offset = 0
                    try:
                        timeout = float((query.get("timeout") or ["0"])[0])
                    except ValueError:
                        timeout = 0.0
                    updates = transport._collect_updates(offset, timeout)
                    with transport._lock:
                        transport._calls.append({"method": "getUpdates", "offset": offset})
                    self._respond({"ok": True, "result": updates})
                    return
                if method == "getMe":
                    with transport._lock:
                        transport._calls.append({"method": "getMe"})
                    self._respond({"ok": True, "result": {"id": 1, "username": "controlled_bot"}})
                    return
                with transport._lock:
                    transport._calls.append({"method": method})
                self._respond({"ok": True, "result": {}})

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                parts = self._parts()
                method = parts[-1] if parts else ""
                payload = self._payload()
                with transport._lock:
                    transport._calls.append({"method": method, "payload": payload,
                                             "chat_id": payload.get("chat_id")})
                if method == "answerCallbackQuery":
                    self._respond({"ok": True, "result": True})
                    return
                if method == "getMe":
                    self._respond({"ok": True, "result": {"id": 1, "username": "controlled_bot"}})
                    return
                if method in {"sendMessage", "sendVideo", "sendPhoto"}:
                    self._respond({"ok": True, "result": {"message_id": len(transport._calls)}})
                    return
                self._respond({"ok": True, "result": True})

        return Handler
