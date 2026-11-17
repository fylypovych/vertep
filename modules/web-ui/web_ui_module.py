import os
import json
import logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Optional
import pg8000.native
from web_ui_logic import WebUIModule, UIConfig, UITheme, UINavigation, migrate_existing_web_ui

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/vertep")
MODULE = WebUIModule(DB_URL)


class WebUIHandler(BaseHTTPRequestHandler):
    def _set_headers(self, status: int = 200, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()

    def _json_response(self, data: Any, status: int = 200):
        self._set_headers(status)
        self.wfile.write(json.dumps(data, default=str).encode())

    def _parse_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path == "/health":
                self._json_response({"status": "ok"})
            elif path == "/api/v1/config":
                configs = MODULE.list_configs()
                self._json_response([c.__dict__ for c in configs])
            elif path.startswith("/api/v1/config/") and path.count("/") == 3:
                key = path.split("/")[3]
                config = MODULE.get_config(key)
                if config:
                    self._json_response(config.__dict__)
                else:
                    self._json_response({"error": "Config not found"}, 404)
            elif path == "/api/v1/themes":
                themes = MODULE.list_themes()
                self._json_response([t.__dict__ for t in themes])
            elif path == "/api/v1/themes/default":
                theme = MODULE.get_default_theme()
                if theme:
                    self._json_response(theme.__dict__)
                else:
                    self._json_response({"error": "No default theme"}, 404)
            elif path.startswith("/api/v1/themes/") and path.count("/") == 3:
                theme_id = path.split("/")[3]
                theme = MODULE.get_theme(theme_id)
                if theme:
                    self._json_response(theme.__dict__)
                else:
                    self._json_response({"error": "Theme not found"}, 404)
            elif path == "/api/v1/navigation":
                visible_only = query.get("visible_only", ["true"])[0].lower() == "true"
                nav = MODULE.list_navigation(visible_only=visible_only)
                self._json_response([n.__dict__ for n in nav])
            elif path.startswith("/api/v1/navigation/") and path.count("/") == 3:
                nav_id = path.split("/")[3]
                nav = MODULE.get_navigation(nav_id)
                if nav:
                    self._json_response(nav.__dict__)
                else:
                    self._json_response({"error": "Navigation item not found"}, 404)
            elif path == "/api/v1/modules":
                modules = MODULE.get_module_info()
                self._json_response(modules)
            else:
                self._json_response({"error": "Not found"}, 404)
        except Exception as e:
            logger.exception("GET error")
            self._json_response({"error": str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._parse_body()

        try:
            if path == "/api/v1/config":
                key = body.get("key")
                value = body.get("value", {})
                description = body.get("description", "")
                if not key:
                    self._json_response({"error": "key required"}, 400)
                    return
                config = MODULE.set_config(key, value, description)
                self._json_response(config.__dict__, 201)
            elif path == "/api/v1/themes":
                theme = UITheme(
                    id=body.get("id", f"theme_{__import__('uuid').uuid4().hex[:12]}"),
                    name=body.get("name"),
                    colors=body.get("colors", {}),
                    is_default=body.get("is_default", False),
                    created_at=body.get("created_at") or __import__("datetime").datetime.utcnow().isoformat(),
                )
                created = MODULE.create_theme(theme)
                self._json_response(created.__dict__, 201)
            elif path == "/api/v1/navigation":
                nav = UINavigation(
                    id=body.get("id", f"nav_{__import__('uuid').uuid4().hex[:12]}"),
                    label=body.get("label"),
                    path=body.get("path"),
                    icon=body.get("icon"),
                    order=body.get("order", 0),
                    module_namespace=body.get("module_namespace"),
                    required_permissions=body.get("required_permissions", []),
                    visible=body.get("visible", True),
                )
                created = MODULE.create_navigation(nav)
                self._json_response(created.__dict__, 201)
            elif path == "/api/v1/migrate":
                result = migrate_existing_web_ui(DB_URL, body.get("data", {}))
                self._json_response(result)
            else:
                self._json_response({"error": "Not found"}, 404)
        except ValueError as e:
            self._json_response({"error": str(e)}, 400)
        except Exception as e:
            logger.exception("POST error")
            self._json_response({"error": str(e)}, 500)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._parse_body()

        try:
            if path.startswith("/api/v1/themes/") and path.count("/") == 3:
                theme_id = path.split("/")[3]
                theme = MODULE.update_theme(theme_id, body)
                if theme:
                    self._json_response(theme.__dict__)
                else:
                    self._json_response({"error": "Theme not found"}, 404)
            elif path.startswith("/api/v1/navigation/") and path.count("/") == 3:
                nav_id = path.split("/")[3]
                nav = MODULE.update_navigation(nav_id, body)
                if nav:
                    self._json_response(nav.__dict__)
                else:
                    self._json_response({"error": "Navigation item not found"}, 404)
            else:
                self._json_response({"error": "Not found"}, 404)
        except ValueError as e:
            self._json_response({"error": str(e)}, 400)
        except Exception as e:
            logger.exception("PATCH error")
            self._json_response({"error": str(e)}, 500)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path.startswith("/api/v1/themes/") and path.count("/") == 3:
                theme_id = path.split("/")[3]
                if MODULE.delete_theme(theme_id):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Theme not found"}, 404)
            elif path.startswith("/api/v1/navigation/") and path.count("/") == 3:
                nav_id = path.split("/")[3]
                if MODULE.delete_navigation(nav_id):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Navigation item not found"}, 404)
            else:
                self._json_response({"error": "Not found"}, 404)
        except ValueError as e:
            self._json_response({"error": str(e)}, 400)
        except Exception as e:
            logger.exception("DELETE error")
            self._json_response({"error": str(e)}, 500)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


def main():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("", port), WebUIHandler)
    logger.info(f"Web UI module listening on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()