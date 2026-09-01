"""backend.services.agent.run_login 测试: RunLoginManager + StandaloneLoginGate。"""

from __future__ import annotations

import asyncio

import pytest

from conftest import FakeCdpMgr, FakeStream, make_test_app
from backend.services.agent.bridge import BrowserBridge
from backend.services.agent.run_login import RunLoginManager, StandaloneLoginGate
from backend.services.agent.session.event import EventHub


@pytest.fixture()
def hub():
    return EventHub()


@pytest.fixture()
def bridge():
    return BrowserBridge(FakeStream())


@pytest.fixture()
def mgr(hub):
    return RunLoginManager(hub)


def make_gate(hub, bridge, run_id="rid1"):
    return StandaloneLoginGate(run_id, hub, bridge)


# --------------------------------------------------------------------------- Manager


def test_manager_new_get_remove(mgr):
    gate = mgr.new_gate("r1", None)
    assert mgr.get_gate("r1") is gate
    assert mgr.get_gate("missing") is None
    mgr.remove("r1")
    assert mgr.get_gate("r1") is None
    mgr.remove("r1")  # 幂等


def test_manager_remove_cancels_gate(mgr):
    gate = mgr.new_gate("r2", None)
    loop = asyncio.new_event_loop()
    gate._future = loop.create_future()
    mgr.remove("r2")
    assert gate._future.result() == {"cancelled": True}
    loop.close()


# --------------------------------------------------------------------------- 交互主流程


async def test_gate_request_account_and_answer(hub, bridge):
    gate = make_gate(hub, bridge)
    payload = {"qid": "q1", "login_type": "account", "fields": []}
    task = asyncio.create_task(gate.request(payload))
    for _ in range(50):
        if gate._future is not None:
            break
        await asyncio.sleep(0.01)
    assert gate.payload() == payload
    events = [e for e in hub._buffer if e["type"] == "run_login_request"]
    assert events[-1]["run_id"] == "rid1"
    gate.answer({"account": "x"})
    result = await task
    assert result == {"account": "x"}
    assert gate.payload() is None
    assert gate._future is None


async def test_gate_request_qr_auto_complete(hub, bridge):
    # 让 CDP evaluate 返回已跳转 URL, 自动完成扫码
    fake = FakeCdpMgr()
    fake.session.responses["Runtime.evaluate"] = {
        "id": 1,
        "result": {
            "result": {"type": "string", "value": "https://new.example.com/home"}
        },
    }
    stream = FakeStream()
    stream.cdp = fake
    b = BrowserBridge(stream)
    gate = make_gate(hub, b)
    payload = {"qid": "q1", "login_type": "qr", "url": "https://old.example.com/", "timeout": 30}
    task = asyncio.create_task(gate.request(payload))
    try:
        result = await asyncio.wait_for(task, timeout=5)
        assert result["ok"] is True
        assert result["url"] == "https://new.example.com/home"
    finally:
        if not task.done():
            task.cancel()


async def test_gate_answer_no_future(hub, bridge):
    gate = make_gate(hub, bridge)
    with pytest.raises(ValueError):
        gate.answer({"a": 1})


async def test_gate_answer_no_payload(hub, bridge):
    gate = make_gate(hub, bridge)
    loop = asyncio.get_running_loop()
    gate._future = loop.create_future()
    gate._payload = None
    with pytest.raises(ValueError):
        gate.answer({"a": 1})


async def test_gate_cancel(hub, bridge):
    gate = make_gate(hub, bridge)
    loop = asyncio.get_running_loop()
    gate._future = loop.create_future()
    gate.cancel()
    assert gate._future.result() == {"cancelled": True}
    gate.cancel()  # future done → 幂等


async def test_gate_finish(hub, bridge):
    gate = make_gate(hub, bridge)
    await gate.finish("qr", "https://a.com")
    ev = [e for e in hub._buffer if e["type"] == "run_login_success"][-1]
    assert ev["run_id"] == "rid1"
    assert ev["method"] == "qr"
    assert ev["url"] == "https://a.com"


# --------------------------------------------------------------------------- 登录动作


async def test_gate_send_code_ok(hub, bridge):
    gate = make_gate(hub, bridge)
    gate._payload = {"captcha": {"send_selector": "#btn"}}
    result = await gate.send_code()
    assert result["ok"] is True
    ev = [e for e in hub._buffer if e["type"] == "run_login_action"][-1]
    assert ev["action"] == "send_code"


async def test_gate_send_code_no_selector(hub, bridge):
    gate = make_gate(hub, bridge)
    gate._payload = {"captcha": {}}
    result = await gate.send_code()
    assert result["ok"] is False
    assert "未找到" in result["message"]


async def test_gate_refresh_captcha_with_image(hub, bridge):
    gate = make_gate(hub, bridge)
    gate._payload = {"captcha": {"refresh_selector": "img", "image_selector": "img"}}
    result = await gate.refresh_captcha()
    assert result["ok"] is True
    assert result["image"].startswith("data:image/png;base64,")


async def test_gate_refresh_captcha_no_image(hub, bridge):
    gate = make_gate(hub, bridge)
    gate._payload = {"captcha": {}}
    result = await gate.refresh_captcha()
    assert result["ok"] is False
    assert result["image"] is None


async def test_gate_captcha_image_no_selector(hub, bridge):
    gate = make_gate(hub, bridge)
    gate._payload = {"captcha": {}}
    assert await gate._captcha_image() is None


async def test_gate_captcha_image_error(hub, bridge):
    class _BadStream(FakeStream):
        async def screenshot_element(self, selector):
            raise RuntimeError("boom")

    gate = make_gate(hub, BrowserBridge(_BadStream()))
    gate._payload = {"captcha": {"image_selector": "img"}}
    assert await gate._captcha_image() is None


async def test_gate_refresh_qr(hub, bridge):
    gate = make_gate(hub, bridge)
    result = await gate.refresh_qr()
    assert result["ok"] is True
    assert "已刷新" in result["message"]
    ev = [e for e in hub._buffer if e["type"] == "run_login_action"][-1]
    assert ev["action"] == "refresh_qr"


async def test_gate_refresh_qr_fail(hub):
    class _FakeCdp:
        async def evaluate(self, expression, timeout=5.0):
            return {"ok": False, "error": "no page"}

    stream = FakeStream()
    stream.cdp = _FakeCdp()
    gate = make_gate(hub, BrowserBridge(stream))
    result = await gate.refresh_qr()
    assert result["ok"] is False
    assert "失败" in result["message"]


# --------------------------------------------------------------------------- QR 监听


async def test_gate_request_qr_cancels_monitor(hub):
    """用户立即答复 → 请求 finally 取消尚未完成的 QR 监听任务。"""
    stream = FakeStream()
    b = BrowserBridge(stream)
    gate = make_gate(hub, b)
    payload = {"qid": "q1", "login_type": "qr", "url": "https://a.com/", "timeout": 30}
    task = asyncio.create_task(gate.request(payload))
    for _ in range(50):
        if gate._future is not None:
            break
        await asyncio.sleep(0.01)
    gate.answer({"cancelled": True})
    result = await asyncio.wait_for(task, timeout=5)
    assert result == {"cancelled": True}


async def test_gate_qr_monitor_external_answer(hub, monkeypatch):
    """QR 监听醒来后发现 future 已完成, 直接返回。"""
    import backend.services.agent.run_login as run_login_mod

    orig_sleep = asyncio.sleep

    async def fast_sleep(sec):
        await orig_sleep(0.01)

    monkeypatch.setattr(run_login_mod.asyncio, "sleep", fast_sleep)
    stream = FakeStream()
    b = BrowserBridge(stream)
    gate = make_gate(hub, b)
    payload = {"qid": "q1", "login_type": "qr", "url": "https://a.com/", "timeout": 30}
    task = asyncio.create_task(gate.request(payload))
    for _ in range(50):
        if gate._future is not None:
            break
        await orig_sleep(0.01)
    gate.answer({"cancelled": True})
    result = await asyncio.wait_for(task, timeout=5)
    assert result == {"cancelled": True}


async def test_gate_qr_monitor_timeout(hub, monkeypatch):
    """QR 监听超时 → 发 run_login_timeout 事件。"""
    import backend.services.agent.run_login as run_login_mod

    orig_sleep = asyncio.sleep

    async def slow_sleep(sec):
        await orig_sleep(0.05)

    monkeypatch.setattr(run_login_mod.asyncio, "sleep", slow_sleep)
    stream = FakeStream()
    b = BrowserBridge(stream)
    gate = make_gate(hub, b)
    payload = {"qid": "q1", "login_type": "qr", "url": "https://a.com/", "timeout": 0.001}
    loop = asyncio.get_running_loop()
    gate._future = loop.create_future()
    task = asyncio.create_task(gate._monitor_qr(payload))
    try:
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
    ev = [e for e in hub._buffer if e["type"] == "run_login_timeout"]
    assert ev and ev[-1]["run_id"] == "rid1"
