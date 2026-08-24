"""backend.routers.stream 测试: /ws/live、/ws/console|network|dom|storage、/live.mjpg。"""

from __future__ import annotations

import asyncio
import json
import struct

import httpx
import pytest
from fastapi import WebSocketDisconnect

from conftest import FakeStream, FakeWS, make_test_app


# --------------------------------------------------------------------------- /ws/live


async def test_ws_live_hello_and_frame():
    from backend.routers.stream import ws_live

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_live(ws))
    try:
        await asyncio.sleep(0.15)
        assert ws.accepted
        assert len(ws.sent) >= 2
        hello = json.loads(ws.sent[0])
        assert hello["type"] == "hello"
        assert hello["fps"] == app.state.cfg.framerate
        assert hello["width"] == app.state.cfg.width
        frame = ws.sent[1]
        ts = struct.unpack("<d", frame[:8])[0]
        assert ts > 0
        assert frame[8:] == app.state.stream.capture.frame
    finally:
        task.cancel()


async def test_ws_live_frame_push():
    from backend.routers.stream import ws_live

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_live(ws))
    try:
        await asyncio.sleep(0.1)
        n0 = len(ws.sent)
        app.state.stream.capture.push_frame(b"\xff\xd8second")
        for _ in range(50):
            if len(ws.sent) > n0:
                break
            await asyncio.sleep(0.05)
        frame = ws.sent[-1]
        assert frame[8:] == b"\xff\xd8second"
    finally:
        task.cancel()


# --------------------------------------------------------------------------- /ws/console 等


async def test_ws_console():
    from backend.routers.stream import ws_console

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_console(ws))
    try:
        await asyncio.sleep(0.2)
        assert ws.accepted
        assert ws.sent and ws.sent[0] == "hello"
    finally:
        task.cancel()


async def test_ws_network():
    from backend.routers.stream import ws_network

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_network(ws))
    try:
        await asyncio.sleep(0.2)
        assert ws.accepted
        assert ws.sent
    finally:
        task.cancel()


async def test_ws_dom():
    from backend.routers.stream import ws_dom

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_dom(ws))
    try:
        await asyncio.sleep(0.2)
        assert ws.accepted
        assert ws.sent
    finally:
        task.cancel()


async def test_ws_storage():
    from backend.routers.stream import ws_storage

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_storage(ws))
    try:
        await asyncio.sleep(0.2)
        assert ws.accepted
        assert ws.sent
    finally:
        task.cancel()


class _TimeoutSub:
    """wait 立刻返回 None, 覆盖 _channel_pump 的超时继续分支。"""

    async def wait(self, timeout=None):
        await asyncio.sleep(0)  # 让出事件循环, 避免饥饿
        return None


class _TimeoutChannel:
    async def attach(self):
        return _TimeoutSub()

    async def detach(self, sub):
        pass


class _ChanStream(FakeStream):
    def __init__(self):
        super().__init__()
        for attr in ("console", "network", "dom", "storage"):
            obj = type(attr.title(), (), {"channel": _TimeoutChannel()})()
            setattr(self, attr, obj)


async def test_channel_pump_timeout_continue():
    from backend.routers.stream import _channel_pump

    stream = _ChanStream()
    ws = FakeWS(app=stream)
    task = asyncio.create_task(_channel_pump(ws, stream.console.channel))
    try:
        await asyncio.sleep(0.1)
        assert not task.done()  # 超时后 continue 继续轮询, 不退出
    finally:
        task.cancel()


class _PushSub:
    def __init__(self, item="msg"):
        self.item = item

    async def wait(self, timeout=None):
        await asyncio.sleep(0.02)
        return self.item


class _PushChannel:
    async def attach(self):
        return _PushSub()

    async def detach(self, sub):
        pass


async def test_channel_pump_websocket_disconnect():
    from backend.routers.stream import _channel_pump

    class _WS(FakeWS):
        async def send_text(self, data):
            raise WebSocketDisconnect()

    ws = _WS()
    channel = _PushChannel()
    await _channel_pump(ws, channel)  # 应立即返回, 不抛异常


async def test_channel_pump_generic_exception():
    from backend.routers.stream import _channel_pump

    class _WS(FakeWS):
        async def send_text(self, data):
            raise ValueError("boom")

    ws = _WS()
    channel = _PushChannel()
    await _channel_pump(ws, channel)  # except Exception 兜底, 静默返回


async def test_ws_live_timeout_ping(monkeypatch):
    import backend.services.capture as cap_mod
    from backend.routers.stream import ws_live
    from conftest import FakeWS, make_test_app

    orig_sleep = asyncio.sleep

    async def fake_sub_wait(self, timeout=None):
        await orig_sleep(0.001)  # 让出事件循环, 避免饥饿
        raise asyncio.TimeoutError()

    monkeypatch.setattr(cap_mod.Subscriber, "wait", fake_sub_wait)
    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_live(ws))
    try:
        await asyncio.sleep(0.1)
        assert any(json.loads(m).get("type") == "hello" for m in ws.sent)
    finally:
        task.cancel()


async def test_ws_live_send_disconnect():
    from backend.routers.stream import ws_live
    from conftest import FakeWS, make_test_app

    class _WS(FakeWS):
        async def send_bytes(self, data):
            raise WebSocketDisconnect()

    app = make_test_app()
    ws = _WS(app=app)
    task = asyncio.create_task(ws_live(ws))
    try:
        await asyncio.sleep(0.1)
        assert task.done()  # 内层 send 失败 → 外层 except WebSocketDisconnect
    finally:
        task.cancel()


async def test_ws_live_send_generic_exception():
    from backend.routers.stream import ws_live
    from conftest import FakeWS, make_test_app

    class _WS(FakeWS):
        async def send_bytes(self, data):
            raise ValueError("boom")

    app = make_test_app()
    ws = _WS(app=app)
    task = asyncio.create_task(ws_live(ws))
    try:
        await asyncio.sleep(0.1)
        assert task.done()  # 外层 except Exception 兜底
    finally:
        task.cancel()


# --------------------------------------------------------------------------- /live.mjpg


async def test_live_mjpg():
    from starlette.requests import Request

    from backend.routers.stream import live_mjpg

    app = make_test_app()
    req = Request({"type": "http", "method": "GET", "path": "/api/v1/live.mjpg", "app": app, "headers": []})
    resp = await live_mjpg(req)
    assert "multipart/x-mixed-replace" in resp.headers["content-type"]
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk)
        if b"fake-jpeg" in b"".join(chunks):
            break
    body = b"".join(chunks)
    assert b"--frame" in body
    assert b"Content-Type: image/jpeg" in body
    assert b"fake-jpeg" in body


async def test_live_mjpg_fresh_latest():
    """cap.latest 新鲜时先发一帧。"""
    import time as _t

    from starlette.requests import Request

    from backend.routers.stream import live_mjpg

    app = make_test_app()
    app.state.stream.capture.latest = (b"FRESH-JPEG", _t.time())
    req = Request({"type": "http", "method": "GET", "path": "/api/v1/live.mjpg", "app": app, "headers": []})
    resp = await live_mjpg(req)
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk)
        if b"FRESH-JPEG" in b"".join(chunks):
            break
    assert b"FRESH-JPEG" in b"".join(chunks)


async def test_live_mjpg_timeout_then_frame(monkeypatch):
    """sub.wait 超时(item=None)后继续, 下一次收到帧。"""
    import time as _t

    import backend.services.capture as cap_mod
    from starlette.requests import Request

    from backend.routers.stream import live_mjpg

    state = {"calls": 0}

    async def fake_wait(self, timeout=None):
        state["calls"] += 1
        if state["calls"] == 1:
            raise asyncio.TimeoutError()
        return (b"\xff\xd8timeout-jpeg", _t.time())

    monkeypatch.setattr(cap_mod.Subscriber, "wait", fake_wait)
    app = make_test_app()
    app.state.stream.capture.latest = None
    req = Request({"type": "http", "method": "GET", "path": "/api/v1/live.mjpg", "app": app, "headers": []})
    resp = await live_mjpg(req)
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk)
        if b"timeout-jpeg" in b"".join(chunks):
            break
    assert b"timeout-jpeg" in b"".join(chunks)


def test_mjpeg_part():
    from backend.routers.stream import MJPEG_BOUNDARY, _mjpeg_part

    part = _mjpeg_part(b"abcd")
    assert part.startswith(b"--" + MJPEG_BOUNDARY + b"\r\n")
    assert b"Content-Length: 4" in part
    assert part.endswith(b"\r\n")
