import os
import json
import logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Optional
import pg8000.native
from publishing_logic import PublishingModule, PublishingChannel, Publication, PublishingCollaboration, migrate_existing_channels, PLATFORMS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/vertep")
MODULE = PublishingModule(DB_URL)


class PublishingHandler(BaseHTTPRequestHandler):
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
            elif path == "/api/v1/channels":
                brand_id = query.get("brand_id", [None])[0]
                platform = query.get("platform", [None])[0]
                owner = query.get("owner_user_id", [None])[0]
                active_only = query.get("active_only", ["false"])[0].lower() == "true"
                channels = MODULE.list_channels(brand_id=brand_id, platform=platform, owner_user_id=owner, active_only=active_only)
                self._json_response([c.__dict__ for c in channels])
            elif path.startswith("/api/v1/channels/") and path.count("/") == 3:
                channel_id = path.split("/")[3]
                channel = MODULE.get_channel(channel_id)
                if channel:
                    self._json_response(channel.__dict__)
                else:
                    self._json_response({"error": "Channel not found"}, 404)
            elif path.startswith("/api/v1/channels/") and path.endswith("/collaborations"):
                channel_id = path.split("/")[3]
                collabs = MODULE.list_collaborations("channel", channel_id)
                self._json_response([c.__dict__ for c in collabs])
            elif path == "/api/v1/publications":
                brand_id = query.get("brand_id", [None])[0]
                storyboard_id = query.get("storyboard_id", [None])[0]
                owner = query.get("owner_user_id", [None])[0]
                status = query.get("status", [None])[0]
                publications = MODULE.list_publications(brand_id=brand_id, storyboard_id=storyboard_id, owner_user_id=owner, status=status)
                self._json_response([p.__dict__ for p in publications])
            elif path.startswith("/api/v1/publications/") and path.count("/") == 3:
                pub_id = path.split("/")[3]
                pub = MODULE.get_publication(pub_id)
                if pub:
                    self._json_response(pub.__dict__)
                else:
                    self._json_response({"error": "Publication not found"}, 404)
            elif path.startswith("/api/v1/publications/") and path.endswith("/collaborations"):
                pub_id = path.split("/")[3]
                collabs = MODULE.list_collaborations("publication", pub_id)
                self._json_response([c.__dict__ for c in collabs])
            elif path == "/api/v1/platforms":
                self._json_response(PLATFORMS)
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
            if path == "/api/v1/channels":
                channel = PublishingChannel(
                    id=body.get("id"),
                    brand_id=body.get("brand_id", "vertep"),
                    platform=body.get("platform", "youtube"),
                    channel_name=body.get("channel_name", ""),
                    channel_id=body.get("channel_id", ""),
                    credentials=body.get("credentials", {}),
                    active=body.get("active", True),
                    owner_user_id=actor,
                    created_at=body.get("created_at") or __import__("datetime").datetime.utcnow().isoformat(),
                    updated_at=body.get("updated_at") or __import__("datetime").datetime.utcnow().isoformat(),
                )
                created = MODULE.create_channel(channel)
                self._json_response(created.__dict__, 201)
            elif path.startswith("/api/v1/channels/") and path.endswith("/collaborations"):
                channel_id = path.split("/")[3]
                collab = MODULE.add_collaboration("channel", channel_id, body.get("collaborator_user_id"), body.get("permission_level", "read"), actor)
                self._json_response(collab.__dict__, 201)
            elif path == "/api/v1/publications":
                pub = Publication(
                    id=body.get("id"),
                    brand_id=body.get("brand_id", "vertep"),
                    storyboard_id=body.get("storyboard_id", ""),
                    video_path=body.get("video_path", ""),
                    title=body.get("title", ""),
                    description=body.get("description", ""),
                    tags=body.get("tags", []),
                    scheduled_at=body.get("scheduled_at"),
                    status=body.get("status", "pending"),
                    platform_results=body.get("platform_results", {}),
                    owner_user_id=actor,
                    created_at=body.get("created_at") or __import__("datetime").datetime.utcnow().isoformat(),
                    updated_at=body.get("updated_at") or __import__("datetime").datetime.utcnow().isoformat(),
                )
                created = MODULE.create_publication(pub)
                self._json_response(created.__dict__, 201)
            elif path.startswith("/api/v1/publications/") and path.endswith("/publish"):
                pub_id = path.split("/")[3]
                platform_channels = body.get("platform_channels", {})  # {platform: channel_id}
                results = MODULE.publish_to_platforms(pub_id, platform_channels, actor)
                self._json_response(results)
            elif path.startswith("/api/v1/publications/") and path.endswith("/collaborations"):
                pub_id = path.split("/")[3]
                collab = MODULE.add_collaboration("publication", pub_id, body.get("collaborator_user_id"), body.get("permission_level", "read"), actor)
                self._json_response(collab.__dict__, 201)
            elif path == "/api/v1/channels/migrate":
                result = migrate_existing_channels(DB_URL, body.get("channels", []))
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
            if path.startswith("/api/v1/channels/") and path.count("/") == 3:
                channel_id = path.split("/")[3]
                channel = MODULE.update_channel(channel_id, body, actor)
                if channel:
                    self._json_response(channel.__dict__)
                else:
                    self._json_response({"error": "Channel not found"}, 404)
            elif path.startswith("/api/v1/publications/") and path.count("/") == 3:
                pub_id = path.split("/")[3]
                pub = MODULE.update_publication(pub_id, body, actor)
                if pub:
                    self._json_response(pub.__dict__)
                else:
                    self._json_response({"error": "Publication not found"}, 404)
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
            if path.startswith("/api/v1/channels/") and path.count("/") == 3:
                channel_id = path.split("/")[3]
                if MODULE.delete_channel(channel_id, actor):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Channel not found"}, 404)
            elif path.startswith("/api/v1/channels/") and "/collaborations/" in path:
                parts = path.split("/")
                channel_id = parts[3]
                collab_id = int(parts[5])
                if MODULE.remove_collaboration("channel", channel_id, collab_id, actor):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Collaboration not found"}, 404)
            elif path.startswith("/api/v1/publications/") and path.count("/") == 3:
                pub_id = path.split("/")[3]
                if MODULE.delete_publication(pub_id, actor):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Publication not found"}, 404)
            elif path.startswith("/api/v1/publications/") and "/collaborations/" in path:
                parts = path.split("/")
                pub_id = parts[3]
                collab_id = int(parts[5])
                if MODULE.remove_collaboration("publication", pub_id, collab_id, actor):
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
    server = ThreadingHTTPServer(("", port), PublishingHandler)
    logger.info(f"Publishing module listening on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()