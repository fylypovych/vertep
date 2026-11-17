import os
import json
import logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Optional
import pg8000.native
from storyboard_logic import StoryboardModule, Script, Scene, Storyboard, StoryboardCollaboration, migrate_existing_scripts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/vertep")
MODULE = StoryboardModule(DB_URL)


class StoryboardHandler(BaseHTTPRequestHandler):
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
            elif path == "/api/v1/scripts":
                brand_id = query.get("brand_id", [None])[0]
                owner = query.get("owner_user_id", [None])[0]
                active_only = query.get("active_only", ["false"])[0].lower() == "true"
                scripts = MODULE.list_scripts(brand_id=brand_id, owner_user_id=owner, active_only=active_only)
                self._json_response([s.__dict__ for s in scripts])
            elif path.startswith("/api/v1/scripts/") and path.count("/") == 3:
                script_id = path.split("/")[3]
                script = MODULE.get_script(script_id)
                if script:
                    self._json_response(script.__dict__)
                else:
                    self._json_response({"error": "Script not found"}, 404)
            elif path.startswith("/api/v1/scripts/") and path.endswith("/scenes"):
                script_id = path.split("/")[3]
                scenes = MODULE.list_scenes(script_id)
                self._json_response([s.__dict__ for s in scenes])
            elif path.startswith("/api/v1/scripts/") and "/collaborations" in path:
                parts = path.split("/")
                script_id = parts[3]
                collabs = MODULE.list_collaborations("script", script_id)
                self._json_response([c.__dict__ for c in collabs])
            elif path == "/api/v1/scenes":
                script_id = query.get("script_id", [None])[0]
                if script_id:
                    scenes = MODULE.list_scenes(script_id)
                    self._json_response([s.__dict__ for s in scenes])
                else:
                    self._json_response({"error": "script_id required"}, 400)
            elif path.startswith("/api/v1/scenes/") and path.count("/") == 3:
                scene_id = path.split("/")[3]
                scene = MODULE.get_scene(scene_id)
                if scene:
                    self._json_response(scene.__dict__)
                else:
                    self._json_response({"error": "Scene not found"}, 404)
            elif path.startswith("/api/v1/scenes/") and "/collaborations" in path:
                scene_id = path.split("/")[3]
                collabs = MODULE.list_collaborations("scene", scene_id)
                self._json_response([c.__dict__ for c in collabs])
            elif path == "/api/v1/storyboards":
                script_id = query.get("script_id", [None])[0]
                owner = query.get("owner_user_id", [None])[0]
                active_only = query.get("active_only", ["false"])[0].lower() == "true"
                storyboards = MODULE.list_storyboards(script_id=script_id, owner_user_id=owner, active_only=active_only)
                self._json_response([s.__dict__ for s in storyboards])
            elif path.startswith("/api/v1/storyboards/") and path.count("/") == 3:
                sb_id = path.split("/")[3]
                sb = MODULE.get_storyboard(sb_id)
                if sb:
                    self._json_response(sb.__dict__)
                else:
                    self._json_response({"error": "Storyboard not found"}, 404)
            elif path.startswith("/api/v1/storyboards/") and "/collaborations" in path:
                sb_id = path.split("/")[3]
                collabs = MODULE.list_collaborations("storyboard", sb_id)
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
            if path == "/api/v1/scripts":
                script = Script(
                    id=body.get("id"),
                    brand_id=body.get("brand_id", "vertep"),
                    title=body.get("title", ""),
                    description=body.get("description", ""),
                    status=body.get("status", "draft"),
                    script_data=body.get("script_data", {}),
                    owner_user_id=actor,
                    active=body.get("active", True),
                    created_at=body.get("created_at") or __import__("datetime").datetime.utcnow().isoformat(),
                    updated_at=body.get("updated_at") or __import__("datetime").datetime.utcnow().isoformat(),
                )
                created = MODULE.create_script(script)
                self._json_response(created.__dict__, 201)
            elif path.startswith("/api/v1/scripts/") and path.endswith("/scenes"):
                script_id = path.split("/")[3]
                script = MODULE.get_script(script_id)
                if not script:
                    self._json_response({"error": "Script not found"}, 404)
                    return
                if script.owner_user_id != actor:
                    self._json_response({"error": "Only script owner can add scenes"}, 403)
                    return
                scene = Scene(
                    id=body.get("id"),
                    script_id=script_id,
                    scene_number=body.get("scene_number", 1),
                    title=body.get("title", ""),
                    description=body.get("description", ""),
                    scene_data=body.get("scene_data", {}),
                    status=body.get("status", "pending"),
                    generated_assets=body.get("generated_assets", []),
                    created_at=body.get("created_at") or __import__("datetime").datetime.utcnow().isoformat(),
                    updated_at=body.get("updated_at") or __import__("datetime").datetime.utcnow().isoformat(),
                )
                created = MODULE.create_scene(scene)
                self._json_response(created.__dict__, 201)
            elif path.startswith("/api/v1/scripts/") and path.endswith("/collaborations"):
                script_id = path.split("/")[3]
                collab = MODULE.add_collaboration("script", script_id, body.get("collaborator_user_id"), body.get("permission_level", "read"), actor)
                self._json_response(collab.__dict__, 201)
            elif path == "/api/v1/scenes":
                script_id = body.get("script_id")
                if not script_id:
                    self._json_response({"error": "script_id required"}, 400)
                    return
                script = MODULE.get_script(script_id)
                if not script or script.owner_user_id != actor:
                    self._json_response({"error": "Only script owner can add scenes"}, 403)
                    return
                scene = Scene(
                    id=body.get("id"),
                    script_id=script_id,
                    scene_number=body.get("scene_number", 1),
                    title=body.get("title", ""),
                    description=body.get("description", ""),
                    scene_data=body.get("scene_data", {}),
                    status=body.get("status", "pending"),
                    generated_assets=body.get("generated_assets", []),
                    created_at=body.get("created_at") or __import__("datetime").datetime.utcnow().isoformat(),
                    updated_at=body.get("updated_at") or __import__("datetime").datetime.utcnow().isoformat(),
                )
                created = MODULE.create_scene(scene)
                self._json_response(created.__dict__, 201)
            elif path.startswith("/api/v1/scenes/") and path.endswith("/collaborations"):
                scene_id = path.split("/")[3]
                collab = MODULE.add_collaboration("scene", scene_id, body.get("collaborator_user_id"), body.get("permission_level", "read"), actor)
                self._json_response(collab.__dict__, 201)
            elif path == "/api/v1/storyboards":
                script_id = body.get("script_id")
                if not script_id:
                    self._json_response({"error": "script_id required"}, 400)
                    return
                script = MODULE.get_script(script_id)
                if not script or script.owner_user_id != actor:
                    self._json_response({"error": "Only script owner can create storyboards"}, 403)
                    return
                sb = Storyboard(
                    id=body.get("id"),
                    script_id=script_id,
                    name=body.get("name", ""),
                    panels=body.get("panels", []),
                    owner_user_id=actor,
                    active=body.get("active", True),
                    created_at=body.get("created_at") or __import__("datetime").datetime.utcnow().isoformat(),
                    updated_at=body.get("updated_at") or __import__("datetime").datetime.utcnow().isoformat(),
                )
                created = MODULE.create_storyboard(sb)
                self._json_response(created.__dict__, 201)
            elif path.startswith("/api/v1/storyboards/") and path.endswith("/collaborations"):
                sb_id = path.split("/")[3]
                collab = MODULE.add_collaboration("storyboard", sb_id, body.get("collaborator_user_id"), body.get("permission_level", "read"), actor)
                self._json_response(collab.__dict__, 201)
            elif path == "/api/v1/scripts/migrate":
                result = migrate_existing_scripts(DB_URL, body.get("scripts", []))
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
            if path.startswith("/api/v1/scripts/") and path.count("/") == 3:
                script_id = path.split("/")[3]
                script = MODULE.update_script(script_id, body, actor)
                if script:
                    self._json_response(script.__dict__)
                else:
                    self._json_response({"error": "Script not found"}, 404)
            elif path.startswith("/api/v1/scripts/") and path.endswith("/activate"):
                script_id = path.split("/")[3]
                script = MODULE.activate_script(script_id, actor)
                if script:
                    self._json_response(script.__dict__)
                else:
                    self._json_response({"error": "Script not found"}, 404)
            elif path.startswith("/api/v1/scripts/") and path.endswith("/deactivate"):
                script_id = path.split("/")[3]
                script = MODULE.deactivate_script(script_id, actor)
                if script:
                    self._json_response(script.__dict__)
                else:
                    self._json_response({"error": "Script not found"}, 404)
            elif path.startswith("/api/v1/scenes/") and path.count("/") == 3:
                scene_id = path.split("/")[3]
                scene = MODULE.update_scene(scene_id, body, actor)
                if scene:
                    self._json_response(scene.__dict__)
                else:
                    self._json_response({"error": "Scene not found"}, 404)
            elif path.startswith("/api/v1/storyboards/") and path.count("/") == 3:
                sb_id = path.split("/")[3]
                sb = MODULE.update_storyboard(sb_id, body, actor)
                if sb:
                    self._json_response(sb.__dict__)
                else:
                    self._json_response({"error": "Storyboard not found"}, 404)
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
            if path.startswith("/api/v1/scripts/") and path.count("/") == 3:
                script_id = path.split("/")[3]
                if MODULE.delete_script(script_id, actor):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Script not found"}, 404)
            elif path.startswith("/api/v1/scripts/") and "/collaborations/" in path:
                parts = path.split("/")
                script_id = parts[3]
                collab_id = int(parts[5])
                if MODULE.remove_collaboration("script", script_id, collab_id, actor):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Collaboration not found"}, 404)
            elif path.startswith("/api/v1/scenes/") and path.count("/") == 3:
                scene_id = path.split("/")[3]
                if MODULE.delete_scene(scene_id, actor):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Scene not found"}, 404)
            elif path.startswith("/api/v1/scenes/") and "/collaborations/" in path:
                parts = path.split("/")
                scene_id = parts[3]
                collab_id = int(parts[5])
                if MODULE.remove_collaboration("scene", scene_id, collab_id, actor):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Collaboration not found"}, 404)
            elif path.startswith("/api/v1/storyboards/") and path.count("/") == 3:
                sb_id = path.split("/")[3]
                if MODULE.delete_storyboard(sb_id, actor):
                    self._json_response({"deleted": True})
                else:
                    self._json_response({"error": "Storyboard not found"}, 404)
            elif path.startswith("/api/v1/storyboards/") and "/collaborations/" in path:
                parts = path.split("/")
                sb_id = parts[3]
                collab_id = int(parts[5])
                if MODULE.remove_collaboration("storyboard", sb_id, collab_id, actor):
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
    server = ThreadingHTTPServer(("", port), StoryboardHandler)
    logger.info(f"Storyboard module listening on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()