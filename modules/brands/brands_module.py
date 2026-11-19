"""Brands Application Module container entrypoint (#500 AC1/AC2, #502).

Only requests carrying the module token issued by CORE are served. Users come
through the CORE Gateway (``x-vertep-role`` and ``x-vertep-brands``); CORE's own
internal calls carry no user role. Administrators manage every brand; a
moderator reads everything and changes only the brands of its account.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from brands_logic import BrandsError, BrandsStore, Conflict, NotFound, health_check

CORE = os.getenv("VERTEP_CORE_URL", "").rstrip("/")
TOKEN = os.getenv("VERTEP_MODULE_TOKEN", "")
UI_DIR = Path(__file__).with_name("ui")
_lock = threading.Lock()
_state: dict[str, Any] = {"connection": None, "store": None}


def _core_database() -> dict:
    request = urllib.request.Request(CORE + "/api/modules/self/database", headers={"x-vertep-module-token": TOKEN})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _connect():
    with _lock:
        if _state["connection"] is None:
            import pg8000.dbapi
            credentials = _core_database()
            connection = pg8000.dbapi.connect(user=credentials["role"], password=credentials["password"],
                                              host=credentials["host"], port=credentials["port"],
                                              database=credentials["database"])
            connection.autocommit = True
            _state["connection"] = connection
        return _state["connection"]


def store() -> BrandsStore:
    if _state["store"] is None:
        _state["store"] = BrandsStore(_connect)
    return _state["store"]


ROUTE_BRAND = re.compile(r"/brands/([a-z0-9][a-z0-9_-]{1,63})")
ROUTE_BRAND_CHANNELS = re.compile(r"/brands/([a-z0-9][a-z0-9_-]{1,63})/channels")
ROUTE_BRAND_CHANNEL = re.compile(r"/brands/([a-z0-9][a-z0-9_-]{1,63})/channels/([A-Za-z0-9_-]{1,64})")
ROUTE_POLICY = re.compile(r"/brands/([a-z0-9][a-z0-9_-]{1,63})/channels/([A-Za-z0-9_-]{1,64})/policy")
ROUTE_CHANNEL = re.compile(r"/channels/([A-Za-z0-9_-]{1,64})")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body: Any, kind: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("content-length", "0") or 0)
        if not length:
            return {}
        value = json.loads(self.rfile.read(length).decode())
        return value if isinstance(value, dict) else {}

    # -- identity -----------------------------------------------------------------------
    def _from_core(self) -> bool:
        return bool(TOKEN) and hmac.compare_digest(self.headers.get("x-vertep-module-token", ""), TOKEN)

    def _role(self) -> str | None:
        return self.headers.get("x-vertep-role")

    def _system(self) -> bool:
        """CORE's own call: no user role is forwarded by the Gateway."""
        return self._role() is None and self.headers.get("x-vertep-caller") == "core"

    def _admin(self) -> bool:
        return self._role() == "admin" or self._system()

    def _may_edit(self, brand_id: str) -> bool:
        if self._admin():
            return True
        brands = {item.strip() for item in self.headers.get("x-vertep-brands", "").split(",") if item.strip()}
        return self._role() == "moderator" and brand_id in brands

    def _actor(self) -> str:
        return self.headers.get("x-vertep-user") or ("core" if self._system() else "unknown")

    # -- dispatch -----------------------------------------------------------------------
    def _handle(self, method: str) -> None:
        path = urlparse(self.path).path
        if method == "GET" and path == "/health":
            return self._send(200, health_check())
        if not self._from_core():
            return self._send(403, {"error": "only through the CORE gateway"})
        try:
            result = self._route(method, path, parse_qs(urlparse(self.path).query))
        except NotFound as error:
            return self._send(404, {"error": str(error)})
        except PermissionError as error:
            return self._send(403, {"error": str(error)})
        except Conflict as error:
            return self._send(409, {"error": str(error)})
        except (BrandsError, ValueError) as error:
            return self._send(422, {"error": str(error)})
        if result is None:
            return self._send(404, {"error": "not available"})
        status, body = result
        if isinstance(body, bytes):
            return self._send(status, body, "text/html; charset=utf-8")
        return self._send(status, body)

    def _require(self, allowed: bool) -> None:
        if not allowed:
            raise PermissionError("administrator or moderator of this brand required")

    def _route(self, method: str, path: str, query: dict):
        data = store()
        if method == "GET" and path == "/ui/index.html":
            return 200, (UI_DIR / "index.html").read_bytes()
        if path == "/brands":
            if method == "GET":
                return 200, data.list_brands()
            if method == "POST":
                self._require(self._admin())
                return 201, data.create_brand(self._body(), self._actor())
        if path == "/channels" and method == "GET":
            brand = (query.get("brand_id") or [None])[0]
            return 200, data.list_channels(brand)
        if path == "/migrate" and method == "POST":
            self._require(self._admin())
            body = self._body()
            return 200, data.import_legacy(body.get("brands") or [], body.get("channels") or [], self._actor())
        if path == "/audit" and method == "GET":
            self._require(self._admin())
            return 200, data.audit_records((query.get("brand_id") or [None])[0])
        match = ROUTE_POLICY.fullmatch(path)
        if match:
            brand_id, channel_id = match.groups()
            if method == "GET":
                return 200, {"brand_id": brand_id, "channel_id": channel_id,
                             "policy": data.effective_policy(brand_id, channel_id)}
            return None
        match = ROUTE_BRAND_CHANNEL.fullmatch(path)
        if match:
            brand_id, channel_id = match.groups()
            if method == "PUT":  # share an existing channel with this brand
                self._require(self._may_edit(brand_id))
                return 200, data.link_channel(brand_id, channel_id, self._body().get("policy", "inherit"), self._actor())
            return None
        match = ROUTE_BRAND_CHANNELS.fullmatch(path)
        if match:
            brand_id = match.group(1)
            if method == "GET":
                return 200, data.list_channels(brand_id)
            if method == "POST":
                self._require(self._may_edit(brand_id))
                return 201, data.create_channel(brand_id, self._body(), self._actor())
            return None
        match = ROUTE_CHANNEL.fullmatch(path)
        if match:
            channel_id = match.group(1)
            channel = data.get_channel(channel_id)
            if channel is None:
                raise NotFound("Channel not found")
            if method == "GET":
                return 200, channel
            self._require(all(self._may_edit(brand) for brand in channel.get("brands", [])))
            if method == "PUT":
                return 200, data.update_channel(channel_id, self._body(), self._actor())
            if method == "DELETE":
                data.delete_channel(channel_id, self._actor())
                return 200, {"deleted": channel_id}
            return None
        match = ROUTE_BRAND.fullmatch(path)
        if match:
            brand_id = match.group(1)
            if method == "GET":
                brand = data.get_brand(brand_id)
                if brand is None:
                    raise NotFound("Brand not found")
                return 200, brand
            if method == "PUT":
                self._require(self._may_edit(brand_id))
                return 200, data.update_brand(brand_id, self._body(), self._actor())
            if method == "DELETE":
                self._require(self._admin())
                data.delete_brand(brand_id, self._actor())
                return 200, {"deleted": brand_id}
        return None

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
