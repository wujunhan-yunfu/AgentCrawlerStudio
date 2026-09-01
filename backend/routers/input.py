"""远程控制输入路由: /ws/input 双向 WebSocket。

    客户端(实时画面覆盖层) → 服务端: 输入事件 JSON
      {type:"mouse"|"key"|"touch", ...}  见 services/input.py
    服务端 → 客户端:
      {type:"hello", viewport:{width,height}, offset:{x,y}, dpr, enabled}
      {type:"ok"}
      {type:"error", message}
      {type:"disabled", reason}          无活动页面/浏览器
"""

from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services.input import InputInjector

router = APIRouter(tags=["input"])


@router.websocket("/ws/input")
async def ws_input(ws: WebSocket):
    await ws.accept()
    stream = ws.app.state.stream
    injector = getattr(stream, "input", None)
    if injector is None:
        injector = InputInjector(stream)
        stream.input = injector
    await injector.refresh_metrics()
    await ws.send_text(json.dumps({
        "type": "hello",
        "viewport": injector.viewport(),
        "offset": injector.offset(),
        "dpr": 1,
        "enabled": stream.cdp.primary() is not None,
    }))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                await ws.send_text(json.dumps({"type": "error", "message": "非法 JSON"}))
                continue
            if not isinstance(msg, dict):
                await ws.send_text(json.dumps({"type": "error", "message": "消息必须是对象"}))
                continue
            result = await injector.dispatch(msg)
            if result.get("ok"):
                await ws.send_text(json.dumps({"type": "ok"}))
            elif result.get("disabled"):
                await ws.send_text(json.dumps({
                    "type": "disabled", "reason": result.get("error") or "无活动页面",
                }))
            else:
                await ws.send_text(json.dumps({
                    "type": "error", "message": result.get("error") or "操作失败",
                }))
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        pass
