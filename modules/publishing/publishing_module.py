"""Publishing Application Module (#502, owner decision 19.11.2026).

Interface and editor actions only. Channels, brand links and policies belong to
the Brands module; tasks, statuses, receipts and approvals to CORE; the actual
upload to the Worker's platform adapters. The module keeps no database and has
no send path. An editor's action is forwarded to CORE together with the
Gateway-signed user context; CORE verifies it and re-derives the user's rights.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

CORE = os.getenv("VERTEP_CORE_URL", "").rstrip("/")
TOKEN = os.getenv("VERTEP_MODULE_TOKEN", "")
UI_DIR = Path(__file__).with_name("ui")
PUBLISH = re.compile(r"/publications/([A-Za-z0-9_.-]{1,80})/publish")


def _core(method: str, path: str, body: dict | None = None, user_context: str = "") -> tuple[int, object]:
    headers = {"x-vertep-module-token": TOKEN, "content-type": "application/json"}
    if user_context:
        headers["x-vertep-user-context"] = user_context
    request = urllib.request.Request(CORE + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read() or b"{}")
        except ValueError:
            detail = {}
        return error.code, detail
    except (urllib.error.URLError, OSError, ValueError):
        return 503, {"detail": "CORE is unavailable"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body, kind: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _from_core(self) -> bool:
        return bool(TOKEN) and hmac.compare_digest(self.headers.get("x-vertep-module-token", ""), TOKEN)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            return self._send(200, {"ready": True})
        if not self._from_core():
            return self._send(403, {"error": "only through the CORE gateway"})
        if path == "/ui/index.html":
            return self._send(200, (UI_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/publications":
            status, items = _core("GET", "/api/modules/self/publications")
            if status != 200:
                return self._send(status, items)
            role = self.headers.get("x-vertep-role")
            brands = {item.strip() for item in self.headers.get("x-vertep-brands", "").split(",") if item.strip()}
            visible = [item for item in items if role == "admin" or (role == "moderator" and item["brand_id"] in brands)]
            return self._send(200, visible)
        return self._send(404, {"error": "not available"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._from_core():
            return self._send(403, {"error": "only through the CORE gateway"})
        match = PUBLISH.fullmatch(path)
        if not match:
            return self._send(404, {"error": "not available"})
        length = int(self.headers.get("content-length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._send(422, {"error": "invalid JSON"})
        # The verified user context goes back to CORE untouched; CORE decides.
        status, answer = _core("POST", f"/api/modules/self/publications/{quote(match.group(1), safe='')}/publish",
                               {"channels": body.get("channels")}, self.headers.get("x-vertep-user-context", ""))
        return self._send(status, answer)


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
