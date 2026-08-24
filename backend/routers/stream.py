"""实时画面路由: WebSocket 推流 + MJPEG 兼容接口"""

from __future__ import annotations

import asyncio
import json
import struct
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..services.capture import Subscriber

router = APIRouter(tags=["stream"])

MJPEG_BOUNDARY = b"frame"


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    stream = ws.app.state.stream
    sub = Subscriber()
    cap = stream.capture
    cap.attach(sub)
    try:
        await ws.send_text(json.dumps({
            "type": "hello",
            "server_time": time.time(),
            "fps": stream.cfg.framerate,
            "width": stream.cfg.width,
            "height": stream.cfg.height,
        }))
        while True:
            try:
                item = await sub.wait(timeout=30)
            except asyncio.TimeoutError:
                item = None
            if item is None:
                await ws.send_text(json.dumps({
                    "type": "hello",
                    "server_time": time.time(),
                }))
                continue
            jpeg, ts = item
            # 二进制: [float64 捕获时刻(秒, 小端)] + [JPEG]
            await ws.send_bytes(struct.pack("<d", ts) + jpeg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        cap.detach(sub)


@router.websocket("/ws/console")
async def ws_console(ws: WebSocket):
    """浏览器控制台实时同步: 文本 JSON 消息 {kind,level,items,text,url,line,stack,ts}"""
    await ws.accept()
    stream = ws.app.state.stream
    await _channel_pump(ws, stream.console.channel)


@router.websocket("/ws/network")
async def ws_network(ws: WebSocket):
    """网络请求实时记录: {type,op,record}"""
    await ws.accept()
    stream = ws.app.state.stream
    await _channel_pump(ws, stream.network.channel)


@router.websocket("/ws/dom")
async def ws_dom(ws: WebSocket):
    """DOM 变更通知: {type,op:"reload"}"""
    await ws.accept()
    stream = ws.app.state.stream
    await _channel_pump(ws, stream.dom.channel)


@router.websocket("/ws/storage")
async def ws_storage(ws: WebSocket):
    """存储变更通知: {type,op:"storage-changed",...}"""
    await ws.accept()
    stream = ws.app.state.stream
    await _channel_pump(ws, stream.storage.channel)


async def _channel_pump(ws: WebSocket, channel) -> None:
    sub = await channel.attach()
    try:
        while True:
            item = await sub.wait(timeout=30)
            if item is not None:
                await ws.send_text(item)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        await channel.detach(sub)


def _mjpeg_part(frame: bytes) -> bytes:
    return (
        b"--" + MJPEG_BOUNDARY + b"\r\n"
        b"Content-Type: image/jpeg\r\n"
        + f"Content-Length: {len(frame)}\r\n\r\n".encode()
        + frame
        + b"\r\n"
    )


@router.get("/live.mjpg")
async def live_mjpg(request: Request) -> StreamingResponse:
    stream = request.app.state.stream
    sub = Subscriber()
    cap = stream.capture
    cap.attach(sub)

    async def gen():
        try:
            if cap.latest is not None and time.time() - cap.latest[1] < 2.0:
                yield _mjpeg_part(cap.latest[0])
            while True:
                try:
                    item = await sub.wait(timeout=10)
                except asyncio.TimeoutError:
                    item = None
                if item:
                    jpeg, _ = item
                    yield _mjpeg_part(jpeg)
        finally:
            cap.detach(sub)

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY.decode()}")
