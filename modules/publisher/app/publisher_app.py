"""Publisher module container (#510/#511, owner decision 21.11.2026: Publisher runs in its own
container and is released and updated separately, like Monitoring).

It only uploads: the Worker that owns a publish task keeps verifying the approved video
against its delivery contract and keeps the publication ledger, then hands this container
the video bytes and the task's own credentials over the node-local port. Platform uploads
use the same adapters as before (``publishers/``). Tokens an adapter refreshes come back in
the answer so the Worker saves them in CORE; the container itself never talks to CORE.

  GET  /health          module self-test: {"status": "ok", "ready": true, "channels": [...]}
  POST /api/v1/publish  x-vertep-module-token, x-vertep-publish: base64 JSON
                        {"channel", "metadata", "credentials": {"secrets", "oauth"}},
                        body = video bytes  ->  {"result": {...}, "credential_updates": {...}}
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vertep.publisher")

TOKEN = os.getenv("VERTEP_MODULE_TOKEN", "")
INPUTS = Path(os.getenv("PUBLISHER_INPUT_ROOT", "/data/inputs"))
MAX_VIDEO_BYTES = int(os.getenv("PUBLISHER_MAX_VIDEO_BYTES", str(4 * 1024 ** 3)))


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

    def _answer(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            from publishers import LIVE_PUBLISHERS
            # The Worker's supervisor marks the module ready only on "ready": true.
            return self._answer(200, {"status": "ok", "ready": True, "channels": sorted(LIVE_PUBLISHERS)})
        return self._answer(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/api/v1/publish":
            return self._answer(404, {"error": "not found"})
        if not TOKEN or not hmac.compare_digest(self.headers.get("x-vertep-module-token", ""), TOKEN):
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
