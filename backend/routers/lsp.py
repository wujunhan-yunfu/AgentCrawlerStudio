"""LSP 路由: /ws/lsp WebSocket 桥接 + /lsp/info 工作区信息"""

from __future__ import annotations

import asyncio
import traceback

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from ..services.lsp import LspManager, LspSession

router = APIRouter(tags=["lsp"])

_manager = LspManager()


def manager() -> LspManager:
    return _manager


@router.get("/lsp/info")
async def lsp_info(request: Request) -> dict:
    """返回 LSP 工作区信息, 前端据此创建与后端一致的文档 URI。"""
    return manager().info()


@router.websocket("/ws/lsp")
async def ws_lsp(websocket: WebSocket) -> None:
    await websocket.accept()
    session: LspSession | None = None
    try:
        session = await manager().create_session(websocket)
        await session.start()
        await session.pump()
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        # 浏览器/连接关闭导致 receive 被取消, 属正常断开, 不打印堆栈
        pass
    except Exception:
        traceback.print_exc()
    finally:
        if session is not None:
            manager().drop_session(session)
            await session.stop()
