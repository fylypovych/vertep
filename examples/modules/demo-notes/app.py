"""Demo Application Module «demo-notes» (#495): a reference for the module contract.

Not a product feature. It shows, with the smallest amount of code:
* an API declared in the manifest (``/notes``) that accepts only requests
  forwarded by the CORE gateway (``x-vertep-module-token`` == own token);
* its own table in its own PostgreSQL schema, credentials from CORE;
* a background service that asks CORE to run an existing shared capability
  (``text_generation``) and stores the verified result;
* a static UI file and ``/health`` for the Worker supervisor.
"""

import base64
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pg8000.dbapi

CORE = os.environ["VERTEP_CORE_URL"].rstrip("/")
TOKEN = os.environ["VERTEP_MODULE_TOKEN"]
UI = Path(__file__).with_name("ui")
_db_lock = threading.Lock()
_db = {"connection": None}


def core(method, path, body=None):
    request = urllib.request.Request(CORE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"x-vertep-module-token": TOKEN, "content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def database():
    """Connection to the module's own schema; None until CORE has provisioned it."""
    with _db_lock:
        if _db["connection"] is None:
            try:
                credentials = core("GET", "/api/modules/self/database")
            except (urllib.error.URLError, OSError, ValueError):
                return None
            connection = pg8000.dbapi.connect(user=credentials["role"], password=credentials["password"],
                                              host=credentials["host"], port=credentials["port"],
                                              database=credentials["database"])
            connection.autocommit = True
            _db["connection"] = connection
        return _db["connection"]


def query(sql, args=()):
    connection = database()
    if connection is None:
        raise RuntimeError("module database is not provisioned")
    with _db_lock:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, args)
            return cursor.fetchall() if cursor.description else []
        except Exception:
            _db["connection"] = None  # reconnect after a restore/update
            raise


def _text(output):
    """The text artifact of a verified Worker result."""
    artifacts = output.get("artifacts", []) if isinstance(output, dict) else output if isinstance(output, list) else []
    for artifact in artifacts:
        if artifact.get("kind") == "text" and artifact.get("data_base64"):
            return base64.b64decode(artifact["data_base64"]).decode("utf-8")
    return None


def summarizer():
    """Background service: pending notes -> shared text capability -> stored result."""
    while True:
        try:
            for note_id, text in query("SELECT id, text FROM notes WHERE status = 'pending' ORDER BY id LIMIT 5"):
                submitted = core("POST", "/api/modules/self/tasks", {"capability": "text_generation", "task_type": "text",
                                                                    "input": {"topic": f"Summarize: {text}"}})
                query("UPDATE notes SET status = 'submitted', task_id = %s WHERE id = %s", (submitted["task_id"], note_id))
            for note_id, task_id in query("SELECT id, task_id FROM notes WHERE status = 'submitted' ORDER BY id LIMIT 5"):
                state = core("GET", f"/api/modules/self/tasks/{task_id}")
                if state["status"] == "COMPLETED" and _text(state["output"]):
                    query("UPDATE notes SET status = 'done', summary = %s WHERE id = %s", (_text(state["output"]), note_id))
                elif state["status"] in ("FAILED", "CANCELLED"):
                    query("UPDATE notes SET status = 'failed' WHERE id = %s", (note_id,))
        except Exception:
            pass  # database not provisioned yet or CORE unreachable: retry
        time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _from_core(self):
        return hmac.compare_digest(self.headers.get("x-vertep-module-token", ""), TOKEN)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ready": True, "database": database() is not None})
        if self.path == "/ui/index.html":
            return self._send(200, (UI / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/notes" and self._from_core():
            try:
                rows = query("SELECT id, text, status, summary FROM notes ORDER BY id")
            except Exception as error:
                return self._send(503, {"error": str(error)})
            return self._send(200, [{"id": r[0], "text": r[1], "status": r[2], "summary": r[3]} for r in rows])
        self._send(404 if self._from_core() else 403, {"error": "not available"})

    def do_POST(self):
        if self.path != "/notes" or not self._from_core():
            return self._send(403, {"error": "only through the CORE gateway"})
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))) or b"{}")
        if not isinstance(body.get("text"), str) or not body["text"].strip():
            return self._send(422, {"error": "text is required"})
        try:
            note_id = query("INSERT INTO notes (text) VALUES (%s) RETURNING id", (body["text"],))[0][0]
        except Exception as error:
            return self._send(503, {"error": str(error)})
        self._send(201, {"id": note_id, "status": "pending"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=summarizer, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
