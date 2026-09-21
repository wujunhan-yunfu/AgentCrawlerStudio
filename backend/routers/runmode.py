"""运行模式路由: 模式信息与 login 运行状态查询(供 CDC/前端感知模式)。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["runmode"])


@router.get("/mode")
async def mode_info(request: Request) -> dict:
    """返回当前后端运行模式与 crawler_id(前端按模式渲染)。"""
    cfg = request.app.state.cfg
    return {
        "mode": cfg.mode if cfg else "dev",
        "crawler_id": (cfg.crawler_id or "default") if cfg else "default",
        "serve": bool(getattr(cfg, "serve", False)) if cfg else False,
    }


@router.get("/login/status")
async def login_status(request: Request) -> dict:
    """login 模式的运行状态(CDC 轮询判定登录是否完成)。"""
    login_run = getattr(request.app.state, "login_run", None)
    if login_run is None:
        raise HTTPException(status_code=404, detail="login 运行未启动")
    return login_run.status_info()