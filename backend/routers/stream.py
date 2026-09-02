"""实时画面路由: WebSocket 推流 + MJPEG 兼容接口"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from contextlib import suppress

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..services.capture import Subscriber

router = APIRouter(tags=["stream"])

MJPEG_BOUNDARY = b"frame"


class _LiveConn:
    """实时画面连接: 记录客户端 + 被接管信号。

    本平台不是共享平台, 整个页面仅允许一个连接。新页面连入时若已有
    连接, 后端先告知冲突, 由客户端决定取消或接管原连接; 同一页面
    刷新重连(client_id 相同)则静默接管, 不再弹提示。
    """

    def __init__(self, ws: WebSocket, client_id: str = ""):
        self.ws = ws
        self.client_id = client_id
        self.kicked = asyncio.Event()


def _live_hub(stream) -> dict:
    """实时画面单连接注册表(懒创建): {'current': _LiveConn | None}。"""
    hub = getattr(stream, "_live_hub", None)
    if hub is None:
        hub = {"current": None}
        stream._live_hub = hub
    return hub


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    stream = ws.app.state.stream
    cap = stream.capture
    hub = _live_hub(stream)
    client_id = (ws.query_params.get("client_id") or "").strip()
    conn = _LiveConn(ws, client_id)
    if hub["current"] is not None:
        prev = hub["current"]
        if prev.client_id and client_id and prev.client_id == client_id:
            # 同一页面刷新重连: 原连接已随页面卸载失效, 静默接管
            hub["current"] = conn
            prev.kicked.set()
            with suppress(Exception):
                await prev.ws.send_text(json.dumps({"type": "kicked"}))
        else:
            # 其他页面(或无标识): 告知冲突, 等待客户端决定取消或接管
            try:
                await ws.send_text(json.dumps({"type": "conflict"}))
                raw = await ws.receive_text()
                decision = json.loads(raw).get("type")
            except (WebSocketDisconnect, RuntimeError):
                return
            except Exception:  # noqa: BLE001
                return
            if decision != "kick":
                # 取消连接: 原连接保持, 本连接关闭
                with suppress(Exception):
                    await ws.close(code=4000, reason="cancelled")
                return
            # 接管原连接(可能已被更早的新连接接管), 本窗口接管
            old = hub["current"]
            hub["current"] = conn
            if old is not None:
                old.kicked.set()
                with suppress(Exception):
                    await old.ws.send_text(json.dumps({"type": "kicked"}))
    else:
        hub["current"] = conn

    sub = Subscriber()
    cap.attach(sub)
    try:
        await _run_live_stream(ws, stream, sub, conn.kicked)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        if hub["current"] is conn:
            hub["current"] = None
        cap.detach(sub)


async def _run_live_stream(ws: WebSocket, stream, sub: Subscriber, kick: asyncio.Event) -> None:
    """推流循环: hello + 最新帧二进制帧, 被接管或客户端断开时立即退出。"""
    await ws.send_text(json.dumps({
        "type": "hello",
        "server_time": time.time(),
        "fps": stream.cfg.framerate,
        "width": stream.cfg.width,
        "height": stream.cfg.height,
    }))
    recv_task = asyncio.create_task(_client_recv(ws))
    try:
        while True:
            item, code = await _wait_item(sub, kick, recv_task)
            if code == "kick" or code == "disconnect":
                return
            if code == "message":
                continue
            if code == "idle":
                # 客户端(测试替身)接收假超时: 重新武装监听
                recv_task = asyncio.create_task(_client_recv(ws))
                continue
            if item is None:
                await ws.send_text(json.dumps({
                    "type": "hello",
                    "server_time": time.time(),
                }))
                continue
            jpeg, ts = item
            # 二进制: [float64 捕获时刻(秒, 小端)] + [JPEG]
            await ws.send_bytes(struct.pack("<d", ts) + jpeg)
    finally:
        recv_task.cancel()
        with suppress(asyncio.CancelledError):
            await recv_task


async def _client_recv(ws: WebSocket):
    """读取客户端消息; 返回 "message"(有消息) / "disconnect"(断开) / "idle"(假超时)。"""
    try:
        await ws.receive_text()
        return "message"
    except (WebSocketDisconnect, RuntimeError):
        return "disconnect"
    except asyncio.TimeoutError:
        return "idle"
    except Exception:  # noqa: BLE001
        return "disconnect"


async def _wait_item(sub: Subscriber, kick: asyncio.Event, recv_task: asyncio.Task):
    """等待一帧 / 被接管 / 客户端消息或断开, 返回 (item, code)。

    code: "frame" | "timeout" | "kick" | "disconnect" | "message" | "idle"
    """
    item_task = asyncio.create_task(sub.wait(timeout=30))
    kicked_task = asyncio.create_task(kick.wait())
    done = set()
    try:
        done, _ = await asyncio.wait(
            {item_task, kicked_task, recv_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (item_task, kicked_task):
            if not t.done():
                t.cancel()
        # 统一取回结果/异常, 避免任务异常未被消费(含外层被取消时)
        await asyncio.gather(item_task, kicked_task, return_exceptions=True)
    if kick.is_set():
        return None, "kick"
    if recv_task in done:
        try:
            return None, recv_task.result()
        except Exception:  # noqa: BLE001
            return None, "disconnect"
    if item_task in done:
        with suppress(asyncio.TimeoutError):
            item = item_task.result()
            if item is not None:
                return item, "frame"
    return None, "timeout"


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
