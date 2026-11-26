"""Unified Publisher module container: editor publications API/UI + platform uploads.

  GET  /health                         module self-test: {"status": "ok", "ready": true, "channels": [...]}
  GET  /ui/index.html                  module UI page (through CORE gateway)
  GET  /publications                   publications visible to the user's brands (via CORE)
  POST /publications/{job_id}/publish  editor publish action with verified user context (via CORE)
  POST /api/v1/publish                 node-local platform upload with task-scoped credentials
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vertep.publisher")

CORE = os.getenv("VERTEP_CORE_URL", "").rstrip("/")
TOKEN = os.getenv("VERTEP_MODULE_TOKEN", "")
INPUTS = Path(os.getenv("PUBLISHER_INPUT_ROOT", "/data/inputs"))
MAX_VIDEO_BYTES = int(os.getenv("PUBLISHER_MAX_VIDEO_BYTES", str(4 * 1024 ** 3)))
_LOCAL_UI = Path(__file__).resolve().parent / "ui"
UI_DIR = _LOCAL_UI if _LOCAL_UI.is_dir() else Path(__file__).resolve().parents[1] / "ui"
PUBLISH_ACTION = re.compile(r"/publications/([A-Za-z0-9_.-]{1,80})/publish")


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


def publish(channel: str, video_path: str, metadata: dict, credentials: dict) -> tuple[dict, dict]:
    """One upload with task-scoped credentials; returns the adapter result and refreshed tokens."""
    from publishers import LIVE_PUBLISHERS
    from publishers.task_secrets import InMemoryTaskSecrets, task_secrets
    publisher = LIVE_PUBLISHERS.get(channel)
    source = InMemoryTaskSecrets(credentials)
    with task_secrets(source):
        if publisher is None or not publisher.configured():
            return ({"channel": channel, "status": "NOT_CONFIGURED",
                     "error": f"{publisher.credential_env if publisher else 'unknown'} is missing"}, source.updates)
        result = publisher.publish(video_path, metadata)
    return result, source.updates


class Handler(BaseHTTPRequestHandler):
    server_version = "VertepPublisher/1"

    def _answer(self, status: int, body, kind: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _has_token(self) -> bool:
        return bool(TOKEN) and hmac.compare_digest(self.headers.get("x-vertep-module-token", ""), TOKEN)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            from publishers import LIVE_PUBLISHERS
            # The Worker's supervisor marks the module ready only on "ready": true.
            return self._answer(200, {"status": "ok", "ready": True, "channels": sorted(LIVE_PUBLISHERS)})
        if path in {"/ui/index.html", "/publications"}:
            if not self._has_token():
                return self._answer(403, {"error": "only through the CORE gateway"})
            if path == "/ui/index.html":
                return self._answer(200, (UI_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            status, items = _core("GET", "/api/modules/self/publications")
            if status != 200:
                return self._answer(status, items)
            role = self.headers.get("x-vertep-role")
            brands = {item.strip() for item in self.headers.get("x-vertep-brands", "").split(",") if item.strip()}
            visible = [item for item in items if role == "admin" or (role == "moderator" and item["brand_id"] in brands)]
            return self._answer(200, visible)
        return self._answer(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        match = PUBLISH_ACTION.fullmatch(path)
        if match:
            if not self._has_token():
                return self._answer(403, {"error": "only through the CORE gateway"})
            length = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self._answer(422, {"error": "invalid JSON"})
            status, answer = _core(
                "POST",
                f"/api/modules/self/publications/{quote(match.group(1), safe='')}/publish",
                {"channels": body.get("channels")},
                self.headers.get("x-vertep-user-context", ""),
            )
            return self._answer(status, answer)
        if path != "/api/v1/publish":
            return self._answer(404, {"error": "not found"})
        if not self._has_token():
            return self._answer(403, {"error": "module token required"})
        try:
            request = json.loads(base64.b64decode(self.headers.get("x-vertep-publish", ""), validate=True))
            channel = str(request["channel"])
            length = int(self.headers.get("Content-Length", "0"))
        except (ValueError, KeyError, TypeError):
            return self._answer(400, {"error": "invalid publish request"})
        if not 0 < length <= MAX_VIDEO_BYTES:
            return self._answer(413 if length else 400, {"error": "video size is outside the allowed range"})
        try:
            INPUTS.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return self._answer(500, {"error": f"input storage is unavailable: {error.strerror}"})
        video = INPUTS / f"{uuid.uuid4().hex}.mp4"
        # The copy is removed before the answer: once the Worker has the reply, no copy remains.
        try:
            status, answer = self._publish(video, length, channel, request)
        finally:
            video.unlink(missing_ok=True)
        return self._answer(status, answer)

    def _publish(self, video: Path, length: int, channel: str, request: dict) -> tuple[int, dict]:
        remaining = length
        with video.open("wb") as stream:
            while remaining:
                chunk = self.rfile.read(min(remaining, 1024 * 1024))
                if not chunk:
                    return 400, {"error": "video body is incomplete"}
                stream.write(chunk)
                remaining -= len(chunk)
        try:
            result, updates = publish(channel, str(video), request.get("metadata") or {},
                                      request.get("credentials") or {})
        except Exception as error:  # noqa: BLE001 - the Worker decides transient vs permanent
            from core.logging_config import secret_redact
            logger.warning("Publication failed", extra={"channel": channel})
            return 502, {"error": secret_redact(str(error))}
        return 200, {"result": result, "credential_updates": updates}

    def log_message(self, format, *args):  # noqa: A002 - no request lines with secrets in logs
        return


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    logger.info("Publisher module listening", extra={"port": port})
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
