"""Storyboard Application Module (#501 + #508 Scenario Library).

* #501: reads Job Storyboards owned by CORE through the module token and shows
  them scoped to the caller's brands; Job revisions and approvals stay in CORE.
* #508: owns the Scenario Library (independent Scenario Documents, drafts,
  ordered scenes, immutable saved versions, inter-brand sharing and access
  audit) in its own PostgreSQL schema (``mod_storyboard``). Creating a Job from
  a scenario version hands the immutable snapshot to CORE via its versioned
  module API with the Gateway-signed user context.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

try:
    from storyboard_logic import (
        Conflict,
        NotFound,
        ScenarioError,
        ScenarioLibraryStore,
        health_check,
    )
except ImportError:
    from .storyboard_logic import (
        Conflict,
        NotFound,
        ScenarioError,
        ScenarioLibraryStore,
        health_check,
    )

CORE = os.getenv("VERTEP_CORE_URL", "").rstrip("/")
TOKEN = os.getenv("VERTEP_MODULE_TOKEN", "")
UI_DIR = Path(__file__).with_name("ui")

_lock = threading.Lock()
_state: dict[str, Any] = {"connection": None, "store": None}

JOB = re.compile(r"/storyboards/([A-Za-z0-9_.-]{1,80})")
SCENARIO = re.compile(r"/scenarios/([a-z0-9][a-z0-9_-]{1,63})")
SCENARIO_VERSIONS = re.compile(r"/scenarios/([a-z0-9][a-z0-9_-]{1,63})/versions")
SCENARIO_VERSION = re.compile(r"/scenarios/([a-z0-9][a-z0-9_-]{1,63})/versions/([0-9]{1,9})")
SCENARIO_COPY = re.compile(r"/scenarios/([a-z0-9][a-z0-9_-]{1,63})/copy")
SCENARIO_ARCHIVE = re.compile(r"/scenarios/([a-z0-9][a-z0-9_-]{1,63})/archive")
SCENARIO_RESTORE = re.compile(r"/scenarios/([a-z0-9][a-z0-9_-]{1,63})/restore")
SCENARIO_SHARES = re.compile(r"/scenarios/([a-z0-9][a-z0-9_-]{1,63})/shares")
SCENARIO_SHARE_BRAND = re.compile(r"/scenarios/([a-z0-9][a-z0-9_-]{1,63})/shares/([a-z0-9][a-z0-9_-]{1,63})")
SCENARIO_JOBS = re.compile(r"/scenarios/([a-z0-9][a-z0-9_-]{1,63})/jobs")
SCENARIO_AUDIT = re.compile(r"/scenarios/([a-z0-9][a-z0-9_-]{1,63})/audit")


class CoreUnavailable(RuntimeError):
    pass


def _core(path: str):
    """GET from CORE with the module token (kept 1-arg for #501 tests)."""
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


def _core_post(path: str, body: dict, user_context: str = "") -> tuple[int, Any]:
    headers = {"x-vertep-module-token": TOKEN, "content-type": "application/json"}
    if user_context:
        headers["x-vertep-user-context"] = user_context
    request = urllib.request.Request(
        CORE + path,
        method="POST",
        headers=headers,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read() or b"{}")
        except ValueError:
            detail = {"detail": str(error)}
        return error.code, detail
    except (urllib.error.URLError, OSError, ValueError):
        return 503, {"detail": "CORE is unavailable"}


def _core_database() -> dict:
    credentials = _core("/api/modules/self/database")
    if not isinstance(credentials, dict):
        raise CoreUnavailable("Module database is not provisioned")
    return credentials


def _connect():
    with _lock:
        if _state["connection"] is None:
            import pg8000.dbapi
            credentials = _core_database()
            connection = pg8000.dbapi.connect(
                user=credentials["role"],
                password=credentials["password"],
                host=credentials["host"],
                port=credentials["port"],
                database=credentials["database"],
            )
            connection.autocommit = True
            _state["connection"] = connection
        return _state["connection"]


def scenario_store() -> ScenarioLibraryStore:
    if _state["store"] is None:
        _state["store"] = ScenarioLibraryStore(_connect)
    return _state["store"]


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

    def _send(self, status: int, body: Any, kind: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ScenarioError("Invalid JSON body") from error
        if not isinstance(value, dict):
            raise ScenarioError("Request body must be a JSON object")
        return value

    def _from_core(self) -> bool:
        return bool(TOKEN) and hmac.compare_digest(self.headers.get("x-vertep-module-token", ""), TOKEN)

    def _role(self) -> str | None:
        return self.headers.get("x-vertep-role")

    def _system(self) -> bool:
        return self._role() is None and self.headers.get("x-vertep-caller") == "core"

    def _brands(self) -> set[str]:
        raw = self.headers.get("x-vertep-brands", "")
        if raw.strip() == "*":
            return set()
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _actor(self) -> str:
        return self.headers.get("x-vertep-user") or ("core" if self._system() else "unknown")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET" and path == "/health":
            return self._send(200, health_check())
        if not self._from_core():
            return self._send(403, {"error": "only through the CORE gateway"})
        try:
            result = self._route(method, path, query)
        except NotFound as error:
            return self._send(404, {"error": str(error)})
        except PermissionError as error:
            return self._send(403, {"error": str(error)})
        except Conflict as error:
            return self._send(409, {"error": str(error)})
        except ScenarioError as error:
            return self._send(422, {"error": str(error)})
        except CoreUnavailable as error:
            return self._send(503, {"error": str(error)})
        except Exception as error:
            # Drop stale DB connection on unexpected DB failure so next request reconnects.
            with _lock:
                _state["connection"] = None
            return self._send(503, {"error": str(error)})
        if result is None:
            return self._send(404, {"error": "not available"})
        status, body = result
        if isinstance(body, bytes):
            return self._send(status, body, "text/html; charset=utf-8")
        return self._send(status, body)

    def _route(self, method: str, path: str, query: dict[str, list[str]]):
        role = self._role()
        brands = self._brands()
        actor = self._actor()
        system = self._system()
        brand_param = (query.get("brand_id") or [None])[0]

        if method == "GET" and path == "/ui/index.html":
            return 200, (UI_DIR / "index.html").read_bytes()

        # -- #501 Job Storyboards (read-only from CORE) --------------------------
        if method == "GET" and path == "/storyboards":
            items = [item for item in (_core("/api/modules/self/storyboards") or []) if _visible(item, role, brands)]
            if brand_param:
                items = [item for item in items if brand_param in {item["brand_id"], *item["character_owner_brands"]}]
            return 200, items
        match = JOB.fullmatch(path) if method == "GET" else None
        if match:
            item = _core(f"/api/modules/self/storyboards/{quote(match.group(1), safe='')}")
            if item is None or not _visible(item, role, brands):
                raise NotFound("Storyboard not found")
            return 200, item

        # -- #508 Scenario Library (PostgreSQL-backed) ---------------------------
        data = scenario_store()

        if path == "/scenarios":
            if method == "GET":
                scope = (query.get("scope") or ["all"])[0]
                q = (query.get("q") or [None])[0]
                archived = (query.get("archived") or ["false"])[0]
                return 200, data.list_scenarios(
                    role, brands, brand_id=brand_param, scope=scope, q=q, archived=archived, system=system
                )
            if method == "POST":
                body = self._body()
                owner_brand = str(body.get("owner_brand_id") or body.get("brand_id") or brand_param or "").strip()
                if not owner_brand and role == "moderator" and len(brands) == 1:
                    owner_brand = next(iter(brands))
                if not (system or role == "admin" or (role == "moderator" and owner_brand in brands)):
                    raise PermissionError("Brand access required to create a scenario")
                body["owner_brand_id"] = owner_brand
                return 201, data.create_scenario(body, actor)
            return None

        match = SCENARIO_VERSION.fullmatch(path)
        if match and method == "GET":
            sid, ver = match.group(1), int(match.group(2))
            return 200, data.get_version(sid, ver, role, brands, brand_id=brand_param, system=system)

        match = SCENARIO_VERSIONS.fullmatch(path)
        if match:
            sid = match.group(1)
            if method == "GET":
                return 200, data.list_versions(sid, role, brands, brand_id=brand_param, system=system)
            if method == "POST":
                body = self._body()
                return 201, data.save_version(sid, body.get("note", ""), role, brands, actor, system=system)
            return None

        match = SCENARIO_COPY.fullmatch(path)
        if match and method == "POST":
            sid = match.group(1)
            body = self._body()
            target_brand = str(body.get("target_brand_id") or body.get("brand_id") or brand_param or "").strip()
            if not target_brand and role == "moderator" and len(brands) == 1:
                target_brand = next(iter(brands))
            raw_ver = body.get("version")
            ver = int(raw_ver) if raw_ver is not None and str(raw_ver).strip() != "" else None
            return 201, data.copy_scenario(
                sid, target_brand, role, brands, actor, version=ver, title=body.get("title"), system=system
            )

        match = SCENARIO_ARCHIVE.fullmatch(path)
        if match and method == "POST":
            return 200, data.set_archived(match.group(1), True, role, brands, actor, system=system)

        match = SCENARIO_RESTORE.fullmatch(path)
        if match and method == "POST":
            return 200, data.set_archived(match.group(1), False, role, brands, actor, system=system)

        match = SCENARIO_SHARE_BRAND.fullmatch(path)
        if match and method == "DELETE":
            sid, target_brand = match.group(1), match.group(2)
            return 200, {"shares": data.revoke_share(sid, target_brand, role, brands, actor, system=system)}

        match = SCENARIO_SHARES.fullmatch(path)
        if match:
            sid = match.group(1)
            if method == "GET":
                return 200, {"shares": data.list_shares(sid, role, brands, system=system)}
            if method == "POST":
                body = self._body()
                target_brand = str(body.get("brand_id") or "").strip()
                return 200, {"shares": data.grant_share(sid, target_brand, role, brands, actor, system=system)}
            return None

        match = SCENARIO_AUDIT.fullmatch(path)
        if match and method == "GET":
            return 200, data.audit_records(match.group(1), role, brands, system=system)

        match = SCENARIO_JOBS.fullmatch(path)
        if match and method == "POST":
            sid = match.group(1)
            body = self._body()
            raw_ver = body.get("version")
            if raw_ver is None or str(raw_ver).strip() == "":
                raise ScenarioError("version is required to create a Job from a scenario")
            try:
                ver = int(raw_ver)
            except (TypeError, ValueError) as error:
                raise ScenarioError("version must be an integer") from error
            target_brand = str(body.get("brand_id") or brand_param or "").strip()
            if not target_brand and role == "moderator" and len(brands) == 1:
                target_brand = next(iter(brands))
            if not target_brand:
                raise ScenarioError("brand_id is required")
            # Enforce access to this scenario version before handing the snapshot to CORE.
            version_snapshot = data.get_version(sid, ver, role, brands, brand_id=target_brand, system=system)
            if version_snapshot["archived"]:
                raise Conflict("Cannot create a Job from an archived scenario")
            idempotency_key = (
                str(body.get("idempotency_key") or self.headers.get("idempotency-key") or "").strip() or None
            )
            forward_payload = {
                "scenario_id": sid,
                "version": ver,
                "brand_id": target_brand,
                "character_id": body.get("character_id"),
                "topic": body.get("topic"),
                "priority": body.get("priority", 5),
                "task_type": body.get("task_type", "image"),
                "aspect_ratio": body.get("aspect_ratio", "16:9"),
                "output_preset": body.get("output_preset", "youtube"),
                "idempotency_key": idempotency_key,
                "snapshot": version_snapshot,
            }
            return _core_post(
                "/api/modules/self/storyboards/jobs",
                forward_payload,
                self.headers.get("x-vertep-user-context", ""),
            )

        match = SCENARIO.fullmatch(path)
        if match:
            sid = match.group(1)
            if method == "GET":
                return 200, data.get_scenario(sid, role, brands, brand_id=brand_param, system=system)
            if method in {"PUT", "PATCH"}:
                return 200, data.update_draft(sid, self._body(), role, brands, actor, system=system)
            return None

        return None

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
