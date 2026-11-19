"""Storyboard Application Module (#501, owner decision 19.11.2026).

The module has no database of its own: storyboards stay in the jobs in CORE
(versions, revisions, approvals, Worker generation, backup/restore). It reads
them from CORE with its module token and shows them to signed-in users through
the CORE Gateway, scoped to the caller's brands. Decisions on a storyboard stay
in the job's own approval flow; the module does not duplicate them.
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
from urllib.parse import parse_qs, quote, urlparse

CORE = os.getenv("VERTEP_CORE_URL", "").rstrip("/")
TOKEN = os.getenv("VERTEP_MODULE_TOKEN", "")
UI_DIR = Path(__file__).with_name("ui")
JOB = re.compile(r"/storyboards/([A-Za-z0-9_.-]{1,80})")


class CoreUnavailable(RuntimeError):
    pass


def _core(path: str):
    request = urllib.request.Request(CORE + path, headers={"x-vertep-module-token": TOKEN})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise CoreUnavailable(f"CORE answered {error.code}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise CoreUnavailable("CORE is unavailable") from error


def _visible(item: dict, role: str | None, brands: set[str]) -> bool:
    """Administrators see every storyboard; a moderator the ones of its brands,
    including material of other brands that uses its characters."""
    if role == "admin":
        return True
    involved = {item.get("brand_id"), *item.get("character_owner_brands", [])}
    return role == "moderator" and bool(involved & brands)


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

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            return self._send(200, {"ready": True})
        if not TOKEN or not hmac.compare_digest(self.headers.get("x-vertep-module-token", ""), TOKEN):
            return self._send(403, {"error": "only through the CORE gateway"})
        if path == "/ui/index.html":
            return self._send(200, (UI_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        role = self.headers.get("x-vertep-role")
        brands = {item.strip() for item in self.headers.get("x-vertep-brands", "").split(",") if item.strip()}
        try:
            if path == "/storyboards":
                wanted = (parse_qs(urlparse(self.path).query).get("brand_id") or [None])[0]
                items = [item for item in (_core("/api/modules/self/storyboards") or []) if _visible(item, role, brands)]
                if wanted:
                    items = [item for item in items if wanted in {item["brand_id"], *item["character_owner_brands"]}]
                return self._send(200, items)
            match = JOB.fullmatch(path)
            if match:
                item = _core(f"/api/modules/self/storyboards/{quote(match.group(1), safe='')}")
                if item is None or not _visible(item, role, brands):
                    return self._send(404, {"error": "Storyboard not found"})
                return self._send(200, item)
        except CoreUnavailable as error:
            return self._send(503, {"error": str(error)})
        return self._send(404, {"error": "not available"})


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
