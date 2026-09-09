"""控制台页面路由: 网页入口"""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from ..config import STATIC_DIR

router = APIRouter(tags=["console"])


def web_prefix(request: Request) -> str:
    cfg = getattr(request.app.state, "cfg", None)
    return getattr(cfg, "web_prefix", "/") or "/"


def api_prefix(request: Request) -> str:
    cfg = getattr(request.app.state, "cfg", None)
    return getattr(cfg, "api_prefix", "/api/v1")


def _inject_meta(html: str, prefix: str) -> str:
    tag = f'<meta name="acs-api-prefix" content="{escape(prefix, quote=True)}">'
    return html.replace("<head>", f"<head>{tag}", 1)


@router.get("/")
async def index(request: Request) -> Response:
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        return FileResponse(index_path)
    html = index_path.read_text(encoding="utf-8")
    wprefix = web_prefix(request)
    if wprefix != "/":
        html = html.replace('="/assets/', f'="{wprefix}/assets/')
    html = _inject_meta(html, api_prefix(request))
    return HTMLResponse(html)
