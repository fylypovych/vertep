"""Characters Application Module container entrypoint (#499).

Runs in isolated container, accepts only requests with x-vertep-module-token.
Provides API for character management: ownership, collaboration, activation, references.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .characters_logic import CharactersModule, health_check

CORE = os.environ["VERTEP_CORE_URL"].rstrip("/")
TOKEN = os.environ["VERTEP_MODULE_TOKEN"]
UI_DIR = Path(__file__).with_name("ui")
_DB_LOCK = threading.Lock()
_DB: dict[str, Any] = {"connection": None}


def _core(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        CORE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-vertep-module-token": TOKEN, "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _verify_token(request_headers: dict) -> bool:
    return hmac.compare_digest(request_headers.get("x-vertep-module-token", ""), TOKEN)


def _get_brand_id(request_headers: dict, query: str = "") -> str | None:
    """The brand the request acts for: the explicit ``brand_id`` query parameter.

    The CORE gateway's ``x-vertep-user`` is a person, never a brand: it is not
    used as a brand identity (#499).
    """
    from urllib.parse import parse_qs
    values = parse_qs(query).get("brand_id") or []
    brand = values[0].strip() if values else ""
    return brand or None


def _may_change(request_headers: dict, brand_id: str | None) -> bool:
    """Changes need the administrator, or a moderator of the acting brand.

    The CORE gateway forwards the role and the caller's brands
    (``x-vertep-brands``: ``*`` for the administrator).
    """
    if request_headers.get("x-vertep-role") == "admin":
        return True
    if request_headers.get("x-vertep-role") != "moderator" or not brand_id:
        return False
    brands = {item.strip() for item in request_headers.get("x-vertep-brands", "").split(",") if item.strip()}
    return brand_id in brands


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict | bytes, kind: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _from_core(self) -> bool:
        return _verify_token(self.headers)

    def _route(self) -> str:
        return self.path.split("?", 1)[0]

    def _get_brand(self) -> str | None:
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        return _get_brand_id(self.headers, query)

    def _read_body(self) -> dict:
        length = int(self.headers.get("content-length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def do_GET(self) -> None:
        path_ = self._route()
        if path_ == "/health":
            return self._send(200, health_check())
        if not self._from_core():
            return self._send(403, {"error": "only through the CORE gateway"})

        brand_id = self._get_brand()

        if path_ == "/ui/index.html":
            return self._send(200, (UI_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")

        # List characters
        if path_ == "/characters":
            try:
                module = CharactersModule()
                chars = module.list_characters(brand_id)
                return self._send(200, chars)
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Character references
        if path_.startswith("/characters/") and path_.endswith("/references"):
            parts = path_.split("/")
            char_id = parts[2]
            try:
                module = CharactersModule()
                refs = module.get_character(char_id, brand_id)
                if not refs:
                    return self._send(404, {"error": "Character not found"})
                return self._send(200, refs.get("references", []))
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Character collaborations - deprecated, kept for backward compatibility
        if path_.startswith("/characters/") and "/collaborations" in path_:
            parts = path_.split("/")
            char_id = parts[2]
            try:
                module = CharactersModule()
                char = module.get_character(char_id, brand_id)
                if not char:
                    return self._send(404, {"error": "Character not found"})
                # AC3: Active characters are available to all brands, no allowlist needed
                return self._send(200, [])
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Check generation allowed
        if path_.startswith("/characters/") and "/can-generate" in path_:
            parts = path_.split("/")
            char_id = parts[2]
            try:
                module = CharactersModule()
                allowed, reasons = module.check_generation_allowed(char_id, brand_id)
                return self._send(200, {"allowed": allowed, "reasons": reasons,
                                        "owner_brand_id": getattr(module, "last_owner", None)})
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Reference file
        if path_.startswith("/characters/") and "/references/" in path_ and not path_.endswith("/references"):
            parts = path_.split("/")
            char_id = parts[2]
            filename = parts[4]
            try:
                module = CharactersModule()
                char = module.get_character(char_id, brand_id)
                if not char:
                    return self._send(404, {"error": "Character not found"})
                ref_path = module.characters_root / char_id / "references" / filename
                if not ref_path.is_file():
                    return self._send(404, {"error": "Reference not found"})
                return self._send(200, ref_path.read_bytes(), "image/png")
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Get single character after its nested routes.
        if path_.startswith("/characters/") and path_.count("/") == 2:
            char_id = path_.split("/")[2]
            try:
                module = CharactersModule()
                char = module.get_character(char_id, brand_id)
                if not char:
                    return self._send(404, {"error": "Character not found"})
                return self._send(200, char)
            except Exception as error:
                return self._send(503, {"error": str(error)})

        self._send(404, {"error": "not available"})

    def do_POST(self) -> None:
        path_ = self._route()
        if not self._from_core():
            return self._send(403, {"error": "only through the CORE gateway"})
        if not _may_change(self.headers, self._get_brand()):
            return self._send(403, {"error": "administrator or moderator of this brand required"})

        brand_id = self._get_brand()
        body = self._read_body()

        if path_ == "/migrate":
            if self.headers.get("x-vertep-role") != "admin":
                return self._send(403, {"error": "administrator role required"})
            default_owner = body.get("default_owner_brand")
            if not isinstance(default_owner, str) or not default_owner.strip():
                return self._send(400, {
                    "error": "default_owner_brand required for migration",
                    "details": "AC6: Explicit owner assignment required."
                })
            try:
                from .characters_logic import migrate_existing_characters
                module = CharactersModule()
                result = migrate_existing_characters(module, module.characters_root, default_owner.strip())
                return self._send(200, result)
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Create character
        if path_ == "/characters":
            try:
                if not body.get("name"):
                    return self._send(422, {"error": "name is required"})
                if not brand_id:
                    return self._send(422, {"error": "brand_id (owner brand) is required"})
                module = CharactersModule()
                char = module.create_character(brand_id, body)
                return self._send(201, char)
            except ValueError as error:
                return self._send(409, {"error": str(error)})
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Upload reference
        if path_.startswith("/characters/") and path_.endswith("/references"):
            parts = path_.split("/")
            char_id = parts[2]
            try:
                # For multipart/form-data, we'd need more complex parsing
                # For now, expect base64 encoded image in body
                if "data_base64" not in body:
                    return self._send(422, {"error": "data_base64 required"})
                if not brand_id:
                    return self._send(422, {"error": "brand_id (owner brand) is required"})
                data = base64.b64decode(body["data_base64"])
                module = CharactersModule()
                result = module.upload_reference(char_id, brand_id, data)
                return self._send(201, result)
            except ValueError as error:
                return self._send(422, {"error": str(error)})
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Add collaboration - deprecated per AC3 (active characters available to all)
        if path_.startswith("/characters/") and path_.endswith("/collaborations"):
            return self._send(410, {"error": "Collaboration allowlist removed per AC3. Active characters are available to all brands."})

        # Activate character
        if path_.startswith("/characters/") and path_.endswith("/activate"):
            parts = path_.split("/")
            char_id = parts[2]
            try:
                if not brand_id:
                    return self._send(422, {"error": "brand_id (owner brand) is required"})
                module = CharactersModule()
                char = module.activate_character(char_id, brand_id)
                return self._send(200, char)
            except ValueError as error:
                return self._send(409, {"error": str(error)})
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Deactivate character
        if path_.startswith("/characters/") and path_.endswith("/deactivate"):
            parts = path_.split("/")
            char_id = parts[2]
            try:
                if not brand_id:
                    return self._send(422, {"error": "brand_id (owner brand) is required"})
                module = CharactersModule()
                char = module.deactivate_character(char_id, brand_id)
                return self._send(200, char)
            except ValueError as error:
                return self._send(409, {"error": str(error)})
            except Exception as error:
                return self._send(503, {"error": str(error)})

        self._send(404, {"error": "not available"})

    def do_PUT(self) -> None:
        path_ = self._route()
        if not self._from_core():
            return self._send(403, {"error": "only through the CORE gateway"})
        if not _may_change(self.headers, self._get_brand()):
            return self._send(403, {"error": "administrator or moderator of this brand required"})

        brand_id = self._get_brand()
        body = self._read_body()

        # Update character
        if path_.startswith("/characters/"):
            char_id = path_.split("/")[2]
            try:
                if not brand_id:
                    return self._send(422, {"error": "brand_id (owner brand) is required"})
                module = CharactersModule()
                char = module.update_character(char_id, brand_id, body)
                return self._send(200, char)
            except ValueError as error:
                return self._send(409, {"error": str(error)})
            except Exception as error:
                return self._send(503, {"error": str(error)})

        self._send(404, {"error": "not available"})

    def do_DELETE(self) -> None:
        path_ = self._route()
        if not self._from_core():
            return self._send(403, {"error": "only through the CORE gateway"})
        if not _may_change(self.headers, self._get_brand()):
            return self._send(403, {"error": "administrator or moderator of this brand required"})

        brand_id = self._get_brand()

        # Delete character
        if path_.startswith("/characters/") and "/references" not in path_ and "/collaborations" not in path_ and "/activate" not in path_ and "/deactivate" not in path_ and "/can-generate" not in path_:
            char_id = path_.split("/")[2]
            try:
                if not brand_id:
                    return self._send(422, {"error": "brand_id (owner brand) is required"})
                module = CharactersModule()
                module.delete_character(char_id, brand_id)
                return self._send(200, {"deleted": char_id})
            except ValueError as error:
                return self._send(409, {"error": str(error)})
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Delete reference
        if path_.startswith("/characters/") and "/references/" in path_ and not path_.endswith("/references"):
            parts = path_.split("/")
            char_id = parts[2]
            filename = parts[4]
            try:
                if not brand_id:
                    return self._send(422, {"error": "brand_id (owner brand) is required"})
                module = CharactersModule()
                module.delete_reference(char_id, brand_id, filename)
                return self._send(200, {"deleted": filename})
            except ValueError as error:
                return self._send(409, {"error": str(error)})
            except Exception as error:
                return self._send(503, {"error": str(error)})

        # Remove collaboration - deprecated per AC3
        if path_.startswith("/characters/") and "/collaborations/" in path_:
            return self._send(410, {"error": "Collaboration allowlist removed per AC3. Active characters are available to all brands."})

        self._send(404, {"error": "not available"})

    def log_message(self, *args) -> None:
        pass


class CharactersModuleApp:
    """Characters Application Module main class."""

    def __init__(self):
        self.module = CharactersModule()

    def health(self) -> dict:
        return health_check()


if __name__ == "__main__":
    # Initialize database on startup
    module = CharactersModule()
    print("Characters Module initialized")

    # Start HTTP server
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    print("Characters Module listening on 0.0.0.0:8080")
    server.serve_forever()