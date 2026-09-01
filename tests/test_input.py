"""backend.services.input / backend.routers.input 测试: 远程控制输入注入。"""

from __future__ import annotations

import asyncio
import json

from conftest import FakeWS, make_test_app


# --------------------------------------------------------------------------- 工具函数


def _modifiers(mods):
    from backend.services.input import _modifiers

    return _modifiers(mods)


def test_modifiers_bitmask():
    assert _modifiers({}) == 0
    assert _modifiers({"ctrl": True}) == 2
    assert _modifiers({"alt": True, "meta": True, "shift": True}) == 1 | 4 | 8
    assert _modifiers("garbage") == 0
    assert _modifiers(None) == 0


class _Stream:
    """极简 stream: 可控 cfg / FakeCdpMgr。"""

    def __init__(self, cfg=None):
        from backend.config import Config

        from conftest import FakeCdpMgr

        self.cfg = cfg or Config()
        self.cdp = FakeCdpMgr()


def _injector(cfg=None):
    from backend.services.input import InputInjector

    stream = _Stream(cfg)
    return stream, InputInjector(stream)


# --------------------------------------------------------------------------- 坐标映射


async def test_metrics_from_get_layout_metrics():
    stream, inj = _injector()
    stream.cdp.session.responses = {
        "Page.getLayoutMetrics": {
            "result": {"cssVisualViewport": {"clientWidth": 1280, "clientHeight": 700}},
        },
    }
    assert await inj.refresh_metrics(force=True)
    assert inj.viewport() == {"width": 1280, "height": 700}
    assert inj.offset() == {"x": 0, "y": 100}
    # 坐标映射: 屏幕 y=500 → 视口 y=400
    vx, vy = inj._to_viewport(640, 500)
    assert (vx, vy) == (640, 400)


async def test_metrics_failure_keeps_defaults():
    stream, inj = _injector()
    # Page.getLayoutMetrics 返回空 result → 保持默认全屏尺寸(调用本身无错)
    stream.cdp.session.responses = {"Page.getLayoutMetrics": {"result": {}}}
    assert await inj.refresh_metrics(force=True)
    assert inj.viewport() == {"width": 1280, "height": 800}
    assert inj.offset() == {"x": 0, "y": 0}


async def test_metrics_refresh_throttled():
    from conftest import FakeCdpSession

    class _MetricsSession(FakeCdpSession):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def command(self, method, params=None, timeout=5.0):
            self.n += 1
            return {"result": {"cssVisualViewport": {"clientWidth": 1280, "clientHeight": 700}}}

    stream, inj = _injector()
    stream.cdp.session = _MetricsSession()
    stream.cdp.primary_session = stream.cdp.session
    await inj.refresh_metrics(force=True)
    await inj.refresh_metrics()
    assert stream.cdp.session.n == 1  # 限频: 第二次直接返回缓存


async def test_to_viewport_clamp():
    stream, inj = _injector()
    inj._offset = (0, 100)
    inj._viewport = (1280, 700)
    assert inj._to_viewport(-50, -30) == (0, 0)
    assert inj._to_viewport(5000, 5000) == (1279, 699)


# --------------------------------------------------------------------------- 鼠标


async def test_mouse_down_dispatch():
    stream, inj = _injector()
    stream.cdp.session.responses = {
        "Page.getLayoutMetrics": {
            "result": {"cssVisualViewport": {"clientWidth": 1280, "clientHeight": 700}},
        },
    }
    result = await inj.dispatch({
        "type": "mouse", "action": "down",
        "x": 640, "y": 500, "button": 0, "buttons": 1,
        "clickCount": 1, "modifiers": {"shift": True},
    })
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.dispatchMouseEvent"
    assert params["type"] == "mousePressed"
    assert params["x"] == 640 and params["y"] == 400
    assert params["button"] == "left"
    assert params["buttons"] == 1
    assert params["clickCount"] == 1
    assert params["modifiers"] == 8


async def test_mouse_up_dispatch_button_right():
    stream, inj = _injector()
    result = await inj.dispatch({
        "type": "mouse", "action": "up", "x": 10, "y": 20, "button": 2, "buttons": 0,
    })
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.dispatchMouseEvent"
    assert params["type"] == "mouseReleased"
    assert params["button"] == "right"
    assert params["buttons"] == 0


async def test_mouse_wheel_dispatch():
    stream, inj = _injector()
    result = await inj.dispatch({
        "type": "mouse", "action": "wheel", "x": 100, "y": 100,
        "deltaX": -10, "deltaY": 120, "modifiers": {"ctrl": True},
    })
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.dispatchMouseEvent"
    assert params["type"] == "mouseWheel"
    assert params["deltaX"] == -10 and params["deltaY"] == 120
    assert params["modifiers"] == 2


async def test_mouse_move_dispatch():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "mouse", "action": "move", "x": 50, "y": 60, "buttons": 0})
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.dispatchMouseEvent"
    assert params["type"] == "mouseMoved"
    assert params["x"] == 50 and params["y"] == 60


async def test_mouse_move_coalesces_concurrent():
    stream, inj = _injector()
    calls = []

    async def fake_dispatch(x, y, buttons, modifiers):
        calls.append((x, y, buttons, modifiers))
        await asyncio.sleep(0.02)

    inj._dispatch_move = fake_dispatch
    results = await asyncio.gather(
        inj.mouse_move(1, 2, 0, 0),
        inj.mouse_move(3, 4, 1, 0),
    )
    # 后到的 move 合流到同一批, 只注入最新位置
    assert results[1] == {"ok": True, "queued": True}
    assert len(calls) == 2
    assert calls[-1] == (3, 4, 1, 0)


async def test_mouse_unknown_action():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "mouse", "action": "fly"})
    assert not result["ok"]
    assert "未知鼠标动作" in result["error"]


async def test_mouse_bad_coords():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "mouse", "action": "down", "x": "abc", "y": 1})
    assert not result["ok"]
    assert "必须是整数" in result["error"]


# --------------------------------------------------------------------------- 键盘


async def test_key_down_with_text():
    stream, inj = _injector()
    result = await inj.dispatch({
        "type": "key", "action": "down", "key": "a", "code": "KeyA",
        "keyCode": 65, "text": "a", "modifiers": {},
    })
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.dispatchKeyEvent"
    assert params["type"] == "keyDown"
    assert params["key"] == "a" and params["code"] == "KeyA"
    assert params["windowsVirtualKeyCode"] == 65
    assert params["text"] == "a"


async def test_key_up_special():
    stream, inj = _injector()
    result = await inj.dispatch({
        "type": "key", "action": "up", "key": "Enter", "code": "Enter", "keyCode": 13,
    })
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.dispatchKeyEvent"
    assert params["type"] == "keyUp"
    assert params["key"] == "Enter"
    assert "text" not in params


async def test_key_char():
    stream, inj = _injector()
    result = await inj.dispatch({
        "type": "key", "action": "char", "key": "你", "code": "", "text": "你",
    })
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.dispatchKeyEvent"
    assert params["type"] == "char"
    assert params["text"] == "你"


async def test_key_char_missing_text():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "key", "action": "char", "key": "a"})
    assert not result["ok"]
    assert "缺少 text" in result["error"]


async def test_key_unknown_action():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "key", "action": "press"})
    assert not result["ok"]


# --------------------------------------------------------------------------- 触摸


async def test_touch_start():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "touch", "action": "start", "x": 100, "y": 200, "id": 0})
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.dispatchTouchEvent"
    assert params["type"] == "touchStart"
    assert params["touchPoints"][0]["x"] == 100 and params["touchPoints"][0]["y"] == 200


async def test_touch_end():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "touch", "action": "end", "x": 100, "y": 200, "id": 0})
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.dispatchTouchEvent"
    assert params["type"] == "touchEnd"
    assert params["touchPoints"] == []


# --------------------------------------------------------------------------- 无会话


async def test_no_session_disabled():
    stream, inj = _injector()
    stream.cdp.primary_session = None
    result = await inj.dispatch({"type": "mouse", "action": "down", "x": 1, "y": 1})
    assert not result["ok"]
    assert result.get("disabled") is True
    assert "无活动页面" in result["error"]


async def test_unknown_type():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "voice"})
    assert not result["ok"]
    assert "未知输入类型" in result["error"]


# --------------------------------------------------------------------------- 文本输入


async def test_text_insert_dispatch():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "text", "action": "insert", "text": "你好，世界\n第二行"})
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.insertText"
    assert params["text"] == "你好，世界\n第二行"


async def test_text_insert_editable_ok():
    stream, inj = _injector()
    stream.cdp.session.responses = {
        "Runtime.evaluate": {"result": {"result": {"value": True}}},
    }
    result = await inj.dispatch({"type": "text", "text": "abc"})
    assert result["ok"]
    assert any(m == "Input.insertText" for m, _ in stream.cdp.session.command_calls)


async def test_text_insert_not_editable():
    stream, inj = _injector()
    stream.cdp.session.responses = {
        "Runtime.evaluate": {"result": {"result": {"value": False}}},
    }
    result = await inj.dispatch({"type": "text", "text": "abc"})
    assert not result["ok"]
    assert "不在可编辑元素" in result["error"]
    assert not any(m == "Input.insertText" for m, _ in stream.cdp.session.command_calls)


async def test_text_insert_eval_failure_proceeds():
    from conftest import FakeCdpSession

    class _EvalFailSession(FakeCdpSession):
        async def command(self, method, params=None, timeout=5.0):
            if method == "Runtime.evaluate":
                raise RuntimeError("boom")
            return {"result": {}}

    stream, inj = _injector()
    stream.cdp.session = _EvalFailSession()
    stream.cdp.primary_session = stream.cdp.session
    result = await inj.dispatch({"type": "text", "text": "abc"})
    assert result["ok"]  # 自检失败不阻塞插入


async def test_text_empty_noop():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "text", "text": ""})
    assert result["ok"]
    assert result.get("noop") is True
    assert not any(m == "Input.insertText" for m, _ in stream.cdp.session.command_calls)


async def test_text_too_long():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "text", "text": "x" * (8 * 1024 + 1)})
    assert not result["ok"]
    assert "过长" in result["error"]


async def test_text_unknown_action():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "text", "action": "paste", "text": "a"})
    assert not result["ok"]
    assert "未知文本动作" in result["error"]


async def test_text_non_string():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "text", "text": 123})
    assert not result["ok"]
    assert "必须是字符串" in result["error"]


async def test_text_compose_preview():
    stream, inj = _injector()
    result = await inj.dispatch({
        "type": "text", "action": "compose", "text": "nǐ",
        "selectionStart": 0, "selectionEnd": 2,
    })
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.imeSetComposition"
    assert params["text"] == "nǐ"
    assert params["selectionStart"] == 0 and params["selectionEnd"] == 2


async def test_text_compose_unsupported_ignored():
    from conftest import FakeCdpSession

    class _FailComposeSession(FakeCdpSession):
        async def command(self, method, params=None, timeout=5.0):
            if method == "Input.imeSetComposition":
                raise RuntimeError("unsupported")
            return {"result": {}}

    stream, inj = _injector()
    stream.cdp.session = _FailComposeSession()
    stream.cdp.primary_session = stream.cdp.session
    result = await inj.dispatch({"type": "text", "action": "compose", "text": "nǐ"})
    assert result["ok"]  # 不支持时静默忽略预览
    assert inj._ime_supported is False


async def test_text_commit_uses_ime():
    stream, inj = _injector()
    result = await inj.dispatch({"type": "text", "action": "commit", "text": "你"})
    assert result["ok"]
    method, params = stream.cdp.session.command_calls[-1]
    assert method == "Input.imeCommitComposition"
    assert params["text"] == "你"


async def test_text_commit_fallback_insert():
    stream, inj = _injector()
    inj._ime_supported = False
    result = await inj.dispatch({"type": "text", "action": "commit", "text": "你"})
    assert result["ok"]
    assert any(m == "Input.insertText" for m, _ in stream.cdp.session.command_calls)


async def test_text_no_session():
    stream, inj = _injector()
    stream.cdp.primary_session = None
    result = await inj.dispatch({"type": "text", "text": "a"})
    assert not result["ok"]
    assert result.get("disabled") is True


# --------------------------------------------------------------------------- 路由 /ws/input


async def test_ws_input_hello():
    from backend.routers.input import ws_input

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_input(ws))
    try:
        await asyncio.sleep(0.1)
        assert ws.accepted
        hello = json.loads(ws.sent[0])
        assert hello["type"] == "hello"
        assert hello["viewport"] == {"width": 1280, "height": 800}
        assert hello["offset"] == {"x": 0, "y": 0}
        assert hello["enabled"] is True
    finally:
        task.cancel()


async def test_ws_input_mouse_dispatch():
    from backend.routers.input import ws_input

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_input(ws))
    try:
        await asyncio.sleep(0.05)
        ws.put_text(json.dumps({
            "type": "mouse", "action": "down", "x": 100, "y": 100,
            "button": 0, "buttons": 1,
        }))
        for _ in range(50):
            if len(ws.sent) >= 2:
                break
            await asyncio.sleep(0.02)
        assert json.loads(ws.sent[-1])["type"] == "ok"
        calls = app.state.stream.cdp.session.command_calls
        assert any(m == "Input.dispatchMouseEvent" for m, _ in calls)
    finally:
        task.cancel()


async def test_ws_input_invalid_json():
    from backend.routers.input import ws_input

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_input(ws))
    try:
        await asyncio.sleep(0.05)
        ws.put_text("{not json")
        for _ in range(50):
            if len(ws.sent) >= 2:
                break
            await asyncio.sleep(0.02)
        msg = json.loads(ws.sent[-1])
        assert msg["type"] == "error"
        assert "非法 JSON" in msg["message"]
    finally:
        task.cancel()


async def test_ws_input_non_object():
    from backend.routers.input import ws_input

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_input(ws))
    try:
        await asyncio.sleep(0.05)
        ws.put_text(json.dumps([1, 2]))
        for _ in range(50):
            if len(ws.sent) >= 2:
                break
            await asyncio.sleep(0.02)
        msg = json.loads(ws.sent[-1])
        assert msg["type"] == "error"
    finally:
        task.cancel()


async def test_ws_input_disabled_no_session():
    from backend.routers.input import ws_input

    app = make_test_app()
    app.state.stream.cdp.primary_session = None
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_input(ws))
    try:
        await asyncio.sleep(0.05)
        hello = json.loads(ws.sent[0])
        assert hello["enabled"] is False
        ws.put_text(json.dumps({"type": "mouse", "action": "down", "x": 1, "y": 1}))
        for _ in range(50):
            if len(ws.sent) >= 2:
                break
            await asyncio.sleep(0.02)
        msg = json.loads(ws.sent[-1])
        assert msg["type"] == "disabled"
        assert "无活动页面" in msg["reason"]
    finally:
        task.cancel()


async def test_ws_input_text_dispatch():
    from backend.routers.input import ws_input

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_input(ws))
    try:
        await asyncio.sleep(0.05)
        ws.put_text(json.dumps({"type": "text", "text": "你好，远程控制"}))
        for _ in range(50):
            if len(ws.sent) >= 2:
                break
            await asyncio.sleep(0.02)
        assert json.loads(ws.sent[-1])["type"] == "ok"
        calls = app.state.stream.cdp.session.command_calls
        assert any(m == "Input.insertText" for m, _ in calls)
    finally:
        task.cancel()


async def test_ws_input_text_not_editable_error():
    from backend.routers.input import ws_input

    app = make_test_app()
    app.state.stream.cdp.session.responses = {
        "Runtime.evaluate": {"result": {"result": {"value": False}}},
    }
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_input(ws))
    try:
        await asyncio.sleep(0.05)
        ws.put_text(json.dumps({"type": "text", "text": "abc"}))
        for _ in range(50):
            if len(ws.sent) >= 2:
                break
            await asyncio.sleep(0.02)
        msg = json.loads(ws.sent[-1])
        assert msg["type"] == "error"
        assert "不在可编辑元素" in msg["message"]
    finally:
        task.cancel()


async def test_ws_input_websocket_disconnect():
    from fastapi import WebSocketDisconnect

    from backend.routers.input import ws_input

    class _WS(FakeWS):
        async def receive_text(self):
            raise WebSocketDisconnect()

    app = make_test_app()
    ws = _WS(app=app)
    await ws_input(ws)  # 应静默返回, 不抛异常
    assert ws.accepted
