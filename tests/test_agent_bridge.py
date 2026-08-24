"""backend.services.agent.bridge.BrowserBridge 测试。"""

from __future__ import annotations

import pytest

from conftest import FakeCdpMgr, FakeStream


@pytest.fixture()
def stream():
    return FakeStream()


@pytest.fixture()
def bridge(stream):
    from backend.services.agent.bridge import BrowserBridge

    return BrowserBridge(stream)


async def test_bridge_navigate(bridge, stream):
    result = await bridge.navigate("https://a.com")
    assert result == {"url": "https://a.com", "title": "Title"}


async def test_bridge_navigate_fallback_title(bridge, stream):
    class _S(FakeStream):
        async def navigate(self, url, new_page=False):
            return {"title": "Title"}

    b = type(bridge)(_S())
    result = await b.navigate("https://a.com")
    assert result == {"url": "https://a.com", "title": "Title"}


async def test_bridge_pages(bridge):
    result = await bridge.pages()
    assert result[0]["id"] == "p1"


async def test_bridge_evaluate(bridge):
    result = await bridge.evaluate("1+1")
    assert result["ok"] is True


async def test_bridge_element_shot(bridge):
    data = await bridge.element_shot("img")
    assert data == b"fake-element-png"


async def test_bridge_analyze_page_ok(bridge, stream):
    stream.cdp.session.responses["Runtime.evaluate"] = {
        "id": 1,
        "result": {"result": {"type": "string", "value": '{"url": "https://a.com", "links": []}'}},
    }
    result = await bridge.analyze_page()
    assert result["ok"] is True
    assert result["analysis"]["url"] == "https://a.com"


async def test_bridge_analyze_page_fail(bridge, stream):
    class _Cdp(FakeCdpMgr):
        async def evaluate(self, expression, timeout=5.0):
            return {"ok": False, "error": "no session"}

    stream.cdp = _Cdp()
    result = await bridge.analyze_page()
    assert result["ok"] is False
    assert "no session" in result["error"]


async def test_bridge_analyze_page_bad_json(bridge):
    result = await bridge.analyze_page()  # evaluate 返回 undefined → 解析失败
    assert result["ok"] is False


async def test_bridge_run_code(bridge, stream):
    result = await bridge.run_code("print(1)")
    assert result["ok"] is True
    assert stream.run_code_calls[-1][0] == "print(1)"
