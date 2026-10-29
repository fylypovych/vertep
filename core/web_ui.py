from __future__ import annotations

from fastapi import Request
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
import os
from fastapi import APIRouter
router = APIRouter()

@router.get("/setup", include_in_schema=False)
def setup_page(request: Request):
    query = f"?{request.url.query}" if request.url.query else ""
    return Response(status_code=307, headers={"Location": f"/v1/setup.html{query}"})


class SPAStaticFiles(StaticFiles):
    """StaticFiles with SPA fallback for client-side routing (Angular v2)."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and scope.get("method") in ("GET", "HEAD") \
                    and not path.startswith("api/"):
                # Let the Angular router handle client-side routes (e.g. /login, /jobs).
                return await super().get_response("index.html", scope)
            raise


@router.get("/admin", include_in_schema=False)
def admin_alias():
    # v2 is now the default at root; keep /admin as a redirect alias.
    return Response(status_code=307, headers={"Location": "/"})


def mount_web_ui(app):
    static_dir = "web-v2/dist/vertep-admin-v2/browser"
    if not os.path.isdir(static_dir):
        static_dir = "web-v2/dist/vertep-admin-v2"
    app.mount("/v1", StaticFiles(directory="web", html=True), name="web-v1")
    app.mount("/", SPAStaticFiles(directory=static_dir, html=True) if os.path.isdir(static_dir) else StaticFiles(directory="web", html=True), name="web-v2")
