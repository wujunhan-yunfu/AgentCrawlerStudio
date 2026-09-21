"""FastAPI 入口: 组装 config / 服务 / 路由, 管理浏览器链路生命周期

运行: uv run python -m backend.main
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from .config import STATIC_DIR, Config, build_config
from .routers import (
    agent,
    console,
    control,
    export,
    input,
    lsp,
    runmode,
    stream,
    versions,
)
from .services.agent.core import (
    AGENT_BACKEND_DIR,
    AGENT_SAVED_DIR,
    agent_real_path,
)
from .services.agent.checkpointer import close_checkpointer, setup_checkpointer
from .services.agent.run_login import RunLoginManager
from .services.agent.runner import AgentManager
from .services.browser import BrowserStream
from .services.runmode.login_mode import LoginRunManager


def create_app(cfg: Config | None = None) -> FastAPI:
    """应用工厂: 创建 BrowserStream 服务并注入各路由(按 mode 决定挂载)。

    - dev:   完整能力(现状, 全部路由);
    - login: 仅 status + 实时画面 + 模式/状态, 自动运行 Mongo HEAD 代码做登录;
    - run:   不构建 Web 应用, 由 main() 直接进入 CLI 执行器。
    """
    cfg = cfg if cfg is not None else build_config()
    mode = (cfg.mode or "dev")
    service = BrowserStream(cfg)
    agent_mgr = AgentManager()
    run_login = RunLoginManager(agent_mgr.hub)
    login_run = LoginRunManager()
    TMP_DIR = Path(__file__).resolve().parent.parent / "tmp"
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    agent_real_path(AGENT_SAVED_DIR).mkdir(parents=True, exist_ok=True)
    agent_real_path(AGENT_BACKEND_DIR).mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.loop = asyncio.get_running_loop()
        try:
            await service.start()
            if service.error:
                raise RuntimeError(f"启动失败: {service.error}")
        except RuntimeError:
            raise
        service.cdp.start()
        app.state.stream = service
        if mode == "dev":
            agent_mgr.setup(cfg, service)
            await setup_checkpointer(cfg)
            app.state.agent = agent_mgr
            app.state.run_login = run_login
        elif mode == "login":
            login_run.setup(cfg, service, agent_mgr.hub)
            app.state.login_run = login_run
            await login_run.start()
        yield
        if mode == "dev":
            await close_checkpointer()
        elif mode == "login":
            login_run.stop()
        await service.cdp.stop()
        await service.stop()

    app = FastAPI(title="AgentCrawlerStudio", lifespan=lifespan)

    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")
        if cfg.web_prefix != "/":
            app.mount(
                f"{cfg.web_prefix}/assets",
                StaticFiles(directory=STATIC_DIR / "assets"),
                name="assets_web",
            )

    app.state.cfg = cfg

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        """就绪探针: 仅当 Xvfb/Chrome/CDP 就绪返回 200(供 K8s probe / CDC 判定)。"""
        stream_ = getattr(request.app.state, "stream", None)
        xvfb = False
        chrome = False
        if stream_ is not None:
            xvfb = stream_.xvfb is not None and stream_.xvfb.poll() is None
            chrome = stream_.chrome is not None and stream_.chrome.poll() is None
        ok = stream_ is not None and not getattr(stream_, "error", None) and xvfb and chrome
        return {
            "ok": ok,
            "mode": mode,
            "crawler_id": (cfg.crawler_id or "default") or "default",
            "xvfb": xvfb,
            "chrome": chrome,
            "cdp": bool(getattr(stream_, "cdp", None)),
        }

    app.include_router(console.router)
    app.include_router(runmode.router, prefix=cfg.api_prefix)
    if mode in ("dev", "login"):
        app.include_router(stream.router, prefix=cfg.api_prefix)
    if mode == "dev":
        app.include_router(control.router, prefix=cfg.api_prefix)
        app.include_router(input.router, prefix=cfg.api_prefix)
        app.include_router(lsp.router, prefix=cfg.api_prefix)
        app.include_router(agent.router, prefix=cfg.api_prefix)
        app.include_router(versions.router, prefix=cfg.api_prefix)
        app.include_router(export.router, prefix=cfg.api_prefix)
    if cfg.web_prefix != "/":
        app.add_api_route(f"{cfg.web_prefix}", console.index, methods=["GET"])
        app.add_api_route(f"{cfg.web_prefix}/", console.index, methods=["GET"])
    return app


def _default_app() -> FastAPI | None:
    cfg = build_config()
    if (cfg.mode or "dev") == "run":
        return None
    return create_app(cfg)


app = _default_app()


def main() -> None:
    import uvicorn

    cfg = build_config()
    if (cfg.mode or "dev") == "run":
        from .services.runmode.run_mode import run_mode_main

        raise SystemExit(run_mode_main(cfg))
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="info")


if __name__ == "__main__":
    main()