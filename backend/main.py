"""FastAPI 入口: 组装 config / 服务 / 路由, 管理浏览器链路生命周期

运行: uv run python -m backend.main
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import STATIC_DIR, Config, build_config
from .routers import agent, console, control, input, lsp, stream, versions
from .services.agent.core import (
    AGENT_BACKEND_DIR,
    AGENT_SAVED_DIR,
    agent_real_path,
)
from .services.agent.checkpointer import close_checkpointer, setup_checkpointer
from .services.agent.run_login import RunLoginManager
from .services.agent.runner import AgentManager
from .services.browser import BrowserStream


def create_app(cfg: Config | None = None) -> FastAPI:
    """应用工厂: 创建 BrowserStream 服务并注入各路由"""
    cfg = cfg if cfg is not None else build_config()
    service = BrowserStream(cfg)
    agent_mgr = AgentManager()
    run_login = RunLoginManager(agent_mgr.hub)
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
        agent_mgr.setup(cfg, service)
        await setup_checkpointer(cfg)
        app.state.agent = agent_mgr
        app.state.run_login = run_login
        yield
        await close_checkpointer()
        await service.cdp.stop()
        await service.stop()

    app = FastAPI(title="AgentCrawlerStudio", lifespan=lifespan)

    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    app.state.cfg = cfg
    app.include_router(console.router)
    app.include_router(control.router, prefix=cfg.api_prefix)
    app.include_router(input.router, prefix=cfg.api_prefix)
    app.include_router(lsp.router, prefix=cfg.api_prefix)
    app.include_router(stream.router, prefix=cfg.api_prefix)
    app.include_router(agent.router, prefix=cfg.api_prefix)
    app.include_router(versions.router, prefix=cfg.api_prefix)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=app.state.cfg.web_host, port=app.state.cfg.web_port, log_level="info")


if __name__ == "__main__":
    main()
