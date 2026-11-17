import os
import json
import logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Optional
import pg8000.native
from brands_logic import BrandsModule, Brand, BrandCollaboration, migrate_existing_brands

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/vertep")
MODULE = BrandsModule(DB_URL)


class BrandsHandler(BaseHTTPRequestHandler):
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

    def _get_actor_user_id(self) -> str:
        return self.headers.get("X-User-ID", "system")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path == "/health":
                self._json_response({"status": "ok"})
            elif path == "/api/v1/brands":
                owner = query.get("owner_user_id", [None])[0]
                active_only = query.get("active_only", ["false"])[0].lower() == "true"
                brands = MODULE.list_brands(owner_user_id=owner, active_only=active_only)
                self._json_response([b.__dict__ for b in brands])
            elif path.startswith("/api/v1/brands/") and path.count("/") == 3:
                brand_id = path.split("/")[3]
                brand = MODULE.get_brand(brand_id)
                if brand:
                    self._json_response(brand.__dict__)
                else:
                    self._json_response({"error": "Brand not found"}, 404)
            elif path.startswith("/api/v1/brands/") and path.endswith("/collaborations"):
                brand_id = path.split("/")[3]
                collabs = MODULE.list_collaborations(brand_id)
                self._json_response([c.__dict__ for c in collabs])
            else:
                self._json_response({"error": "Not found"}, 404)
        except Exception as e:
            logger.exception("GET error")
            self._json_response({"error": str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._parse_body()
        actor = self._get_actor_user_id()

        try:
            if path == "/api/v1/brands":
                brand = Brand(
                    id=body.get("id"),
                    name=body.get("name", ""),
                    description=body.get("description", ""),
                    logo_url=body.get("logo_url"),
                    website=body.get("website"),
                    social_links=body.get("social_links", {}),
                    owner_user_id=actor,
                    active=body.get("active", True),
                    created_at=body.get("created_at") or __import__("datetime").datetime.utcnow().isoformat(),
                    updated_at=body.get("updated_at") or __import__("datetime").datetime.utcnow().isoformat(),
                )
                created = MODULE.create_brand(brand)
                self._json_response(created.__dict__, 201)
            elif path.startswith("/api/v1/brands/") and path.endswith("/collaborations"):
                brand_id = path.split("/")[3]
                collab = MODULE.add_collaboration(
                    brand_id,
                    body.get("collaborator_user_id"),
                    body.get("permission_level", "read"),
                    actor,
                )
                self._json_response(collab.__dict__, 201)
            elif path == "/api/v1/brands/migrate":
                result = migrate_existing_brands(DB_URL, body.get("brands", []))
                self._json_response(result)
            else:
                self._json_response({"error": "Not found"}, 404)
        except PermissionError as e:
            self._json_response({"error": str(e)}, 403)
        except ValueError as e:
            self._json_response({"error": str(e)}, 400)
        except Exception as e:
            logger.exception("POST error")
            self._json_response({"error": str(e)}, 500)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._parse_body()
        actor = self._get_actor_user_id()

        try:
            if path.startswith("/api/v1/brands/") and path.count("/") == 3:
                brand_id = path.split("/")[3]
                brand = MODULE.update_brand(brand_id, body, actor)
                if brand:
                    self._json_response(brand.__dict__)
                else:
                    self._json_response({"error": "Brand not found"}, 404)
            elif path.startswith("/api/v1/brands/") and path.endswith("/activate"):
                brand_id = path.split("/")[3]
                brand = MODULE.activate_brand(brand_id, actor)
                if brand:
                    self._json_response(brand.__dict__)
                else:
                    self._json_response({"error": "Brand not found"}, 404)
            elif path.startswith("/api/v1/brands/") and path.endswith("/deactivate"):
                brand_id = path.split("/")[3]
                brand = MODULE.deactivate_brand(brand_id, actor)
                if brand:
                    self._json_response(brand.__dict__)
                else:
                    self._json_response({"error": "Brand not found"}, 404)
            else:
                self._json_response({"error": "Not found"}, 404)
        except PermissionError as e:
            self._json_response({"error": str(e)}, 403)
        except ValueError as e:
            self._json_response({"error": str(e)}, 400)
        except Exception as e:
            logger.exception("PATCH error")
            self._json_response({"error": str(e)}, 500)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        actor = self._get_actor_user_id()

        try:
            if path.startswith("/api/v1/brands/") and path.count("/") == 3:
                brand_id = path.split("/")[3]
                if MODULE.delete_brand(brand_id, actor):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Brand not found"}, 404)
            elif path.startswith("/api/v1/brands/") and "/collaborations/" in path:
                parts = path.split("/")
                brand_id = parts[3]
                collab_id = int(parts[5])
                if MODULE.remove_collaboration(brand_id, collab_id, actor):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Collaboration not found"}, 404)
            else:
                self._json_response({"error": "Not found"}, 404)
        except PermissionError as e:
            self._json_response({"error": str(e)}, 403)
        except Exception as e:
            logger.exception("DELETE error")
            self._json_response({"error": str(e)}, 500)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


def main():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("", port), BrandsHandler)
    logger.info(f"Brands module listening on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()