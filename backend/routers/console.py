"""控制台页面路由: 网页入口"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ..config import STATIC_DIR

router = APIRouter(tags=["console"])


@router.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
