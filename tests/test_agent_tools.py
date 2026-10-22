"""backend.services.agent.tools 测试。"""

from __future__ import annotations

import pytest

from conftest import FakePage


@pytest.fixture()
def session(cfg):
    from backend.services.agent.session.event import EventHub
    from backend.services.agent.session.model import AgentSession

    return AgentSession(id="s1", crawler_id="c", title="t", hub=EventHub())


class FakeBridge:
    def __init__(self):
        self.navigate_result = {"url": "http://a", "title": "A"}
        self.pages_result = [{"url": "http://a", "title": "A"}]
        self.evaluate_result = {"ok": True, "item": {"v": "42"}}
        self.analyze_result = {"ok": True, "analysis": {"url": "http://a"}}
        self.run_result = {"ok": True, "output": "out", "error": "", "saved": []}
        self.screenshot_result = b"\x89PNG\r\n\x1a\nfake"
        self.calls = []

    async def navigate(self, url, new_page=False):
        self.calls.append(("navigate", url))
        return self.navigate_result

    async def pages(self):
        return self.pages_result

    async def evaluate(self, expression, timeout=10.0):
        self.calls.append(("evaluate", expression))
        return self.evaluate_result

    async def analyze_page(self):
        return self.analyze_result

    async def screenshot(self):
        self.calls.append(("screenshot",))
        return self.screenshot_result

    async def run_code(self, code, login_gate=None, restart=True):
        self.calls.append(("run_code", code, restart))
        return self.run_result


# --------------------------------------------------------------------------- browser tools


def test_browser_navigate(session):
    from backend.services.agent.tools.browser import build_browser_tools

    bridge = FakeBridge()
    tools = {t.name: t for t in build_browser_tools(session, bridge)}
    out = asyncio_run(tools["browser_navigate"].ainvoke({"url": "http://a", "new_page": True}))
    assert "已导航到 http://a" in out
    assert bridge.calls[0] == ("navigate", "http://a")

    bridge.navigate_result = {"url": "http://b", "title": ""}
    out2 = asyncio_run(tools["browser_navigate"].ainvoke({"url": "http://b"}))
    assert "已导航到 http://b" in out2
    assert "(无标题)" in out2


def test_browser_navigate_error(session):
    from backend.services.agent.tools.browser import build_browser_tools

    class Boom:
        async def navigate(self, url, new_page=False):
            raise RuntimeError("nav boom")

    tools = {t.name: t for t in build_browser_tools(session, Boom())}
    out = asyncio_run(tools["browser_navigate"].ainvoke({"url": "http://a"}))
    assert "导航失败" in out


def test_browser_pages(session):
    from backend.services.agent.tools.browser import build_browser_tools

    bridge = FakeBridge()
    tools = {t.name: t for t in build_browser_tools(session, bridge)}
    out = asyncio_run(tools["browser_pages"].ainvoke({}))
    assert "- A: http://a" in out
    bridge.pages_result = []
    out2 = asyncio_run(tools["browser_pages"].ainvoke({}))
    assert "没有打开" in out2


def test_browser_evaluate(session):
    from backend.services.agent.tools.browser import build_browser_tools

    bridge = FakeBridge()
    tools = {t.name: t for t in build_browser_tools(session, bridge)}
    out = asyncio_run(tools["browser_evaluate"].ainvoke({"expression": "1+1"}))
    assert out == "42"
    bridge.evaluate_result = {"ok": False, "error": "err"}
    out2 = asyncio_run(tools["browser_evaluate"].ainvoke({"expression": "x"}))
    assert "执行失败" in out2


def test_page_analyze(session):
    from backend.services.agent.tools.browser import build_browser_tools

    bridge = FakeBridge()
    tools = {t.name: t for t in build_browser_tools(session, bridge)}
    out = asyncio_run(tools["page_analyze"].ainvoke({}))
    assert '"url": "http://a"' in out
    bridge.analyze_result = {"ok": False, "error": "fail"}
    out2 = asyncio_run(tools["page_analyze"].ainvoke({}))
    assert "页面分析失败" in out2


def test_browser_screenshot(session):
    import base64
    import io

    from PIL import Image

    from backend.services.agent.tools.browser import build_browser_tools

    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000), (10, 20, 30)).save(buf, "PNG")
    bridge = FakeBridge()
    bridge.screenshot_result = buf.getvalue()
    tools = {t.name: t for t in build_browser_tools(session, bridge)}
    out = asyncio_run(tools["browser_screenshot"].ainvoke({}))
    assert isinstance(out, list)
    assert out[0]["type"] == "text"
    assert out[1]["type"] == "image_url"
    assert out[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    raw = out[1]["image_url"]["url"].split(",", 1)[1]
    decoded = base64.b64decode(raw)
    with Image.open(io.BytesIO(decoded)) as im:
        assert im.width == 1280
    assert bridge.calls[-1] == ("screenshot",)


def test_browser_screenshot_fallback_png(session):
    from backend.services.agent.tools.browser import build_browser_tools

    bridge = FakeBridge()
    bridge.screenshot_result = b"not-an-image"
    tools = {t.name: t for t in build_browser_tools(session, bridge)}
    out = asyncio_run(tools["browser_screenshot"].ainvoke({}))
    assert out[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_browser_screenshot_empty(session):
    from backend.services.agent.tools.browser import build_browser_tools

    bridge = FakeBridge()
    bridge.screenshot_result = b""
    tools = {t.name: t for t in build_browser_tools(session, bridge)}
    out = asyncio_run(tools["browser_screenshot"].ainvoke({}))
    assert "截图失败" in out


def test_browser_screenshot_error(session):
    from backend.services.agent.tools.browser import build_browser_tools

    class Boom:
        async def screenshot(self):
            raise RuntimeError("shot boom")

    tools = {t.name: t for t in build_browser_tools(session, Boom())}
    out = asyncio_run(tools["browser_screenshot"].ainvoke({}))
    assert "截图失败" in out


def test_browser_run_code(session):
    from backend.services.agent.tools.browser import build_browser_tools

    bridge = FakeBridge()
    tools = {t.name: t for t in build_browser_tools(session, bridge)}
    out = asyncio_run(tools["browser_run_code"].ainvoke({"code": "print(1)", "restart": False}))
    assert "ok=True" in out
    assert "输出:" in out
    assert bridge.calls[0][2] is False


def test_browser_run_code_saved(session):
    from backend.services.agent.tools.browser import build_browser_tools

    bridge = FakeBridge()
    bridge.run_result = {"ok": True, "output": "o", "error": "", "saved": [{"id": "1"}]}
    tools = {t.name: t for t in build_browser_tools(session, bridge)}
    out = asyncio_run(tools["browser_run_code"].ainvoke({"code": "x"}))
    assert "已保存 1 项" in out


def test_browser_run_code_collapses_progress_frames(session):
    from backend.services.agent.tools.browser import build_browser_tools

    bridge = FakeBridge()
    bridge.run_result = {
        "ok": True,
        "output": "\r抓取:   0%|  | 0/2\r抓取:  50%|# | 1/2\r抓取: 100%|##| 2/2\n结果: 2 条\n",
        "error": "",
        "saved": [],
    }
    tools = {t.name: t for t in build_browser_tools(session, bridge)}
    out = asyncio_run(tools["browser_run_code"].ainvoke({"code": "x"}))
    # 只保留 tqdm 最后一帧, 且最终结果可见
    assert "抓取: 100%|##| 2/2" in out
    assert "抓取:   0%" not in out
    assert "结果: 2 条" in out


# --------------------------------------------------------------------------- http tools


def test_http_request(monkeypatch):
    from backend.services.agent.tools.http import build_http_tools

    class FakeResp:
        status_code = 200
        request = _FakeRequest()
        headers = _HeadersLike({"ct": "text"})
        text = "<html>hi</html>"

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, headers=None, params=None, content=None):
            return FakeResp()

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
    tools = {t.name: t for t in build_http_tools()}
    out = asyncio_run(tools["http_request"].ainvoke({
        "method": "get", "url": "http://a", "headers": '{"h":"v"}',
        "params": '{"p":"1"}', "data": "body"}))
    assert "状态码: 200" in out
    assert "x-test" in out


class _FakeRequest:
    def __init__(self):
        self.headers = _HeadersLike({"x-test": "1"})


def test_http_request_error(monkeypatch):
    from backend.services.agent.tools.http import build_http_tools

    class BoomClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **kw):
            raise RuntimeError("net down")

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "AsyncClient", BoomClient)
    tools = {t.name: t for t in build_http_tools()}
    out = asyncio_run(tools["http_request"].ainvoke({
        "method": "get", "url": "http://a", "headers": "not json", "params": "{}"}))
    assert "HTTP 请求失败" in out


class _HeadersLike(dict):
    pass


def test_api_request_json(monkeypatch):
    from backend.services.agent.tools.http import build_http_tools

    captured = {}

    class FakeResp:
        status_code = 201
        reason_phrase = "Created"
        url = "https://api.test/v1/items?page=1"
        headers = _HeadersLike({"content-type": "application/json; charset=utf-8"})
        request = _FakeRequest()
        el = type("E", (), {"total_seconds": lambda self: 0.123})()
        elapsed = el

        def json(self):
            return {"ok": True, "id": 7}

    class FakeClient:
        def __init__(self, **kw):
            captured["client"] = kw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            captured["kw"] = kw
            return FakeResp()

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
    tools = {t.name: t for t in build_http_tools()}
    out = asyncio_run(tools["api_request"].ainvoke({
        "url": "https://api.test/v1/items", "method": "post",
        "headers": '{"Authorization": "Bearer x"}',
        "params": '{"page": 1}',
        "json_body": '{"name": "n"}',
        "timeout": 5,
    }))
    assert captured["method"] == "POST"
    assert captured["kw"]["json"] == {"name": "n"}
    assert captured["kw"]["headers"] == {"Authorization": "Bearer x"}
    assert captured["kw"]["params"] == {"page": 1}
    assert captured["client"]["timeout"] == 5
    assert captured["client"]["follow_redirects"] is True
    assert "状态码: 201 Created" in out
    assert "耗时: 123 ms" in out
    assert '"id": 7' in out


def test_api_request_form_and_binary(monkeypatch):
    from backend.services.agent.tools.http import build_http_tools

    captured = {}

    class FakeResp:
        status_code = 200
        reason_phrase = "OK"
        url = "https://api.test/form"
        headers = _HeadersLike({"content-type": "image/png"})
        history = []
        request = _FakeRequest()
        elapsed = None
        content = b"\x89PNG"

        def json(self):
            raise ValueError("not json")

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            captured["kw"] = kw
            return FakeResp()

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
    tools = {t.name: t for t in build_http_tools()}
    out = asyncio_run(tools["api_request"].ainvoke({
        "url": "https://api.test/form", "form": '{"a": 1, "b": "2"}'}))
    assert captured["kw"]["data"] == {"a": "1", "b": "2"}
    assert "二进制响应" in out
    assert "4 字节" in out


def test_api_request_errors(monkeypatch):
    from backend.services.agent.tools.http import build_http_tools

    tools = {t.name: t for t in build_http_tools()}

    out = asyncio_run(tools["api_request"].ainvoke({"url": "https://a", "method": "trace"}))
    assert "不支持的 method" in out

    out = asyncio_run(tools["api_request"].ainvoke({"url": "ftp://a"}))
    assert "http:// 或 https://" in out

    out = asyncio_run(tools["api_request"].ainvoke({"url": "https://a", "headers": "[]"}))
    assert "参数解析错误" in out

    class BoomClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **kw):
            raise RuntimeError("net down")

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "AsyncClient", BoomClient)
    out = asyncio_run(tools["api_request"].ainvoke({"url": "https://a"}))
    assert "HTTP 接口请求失败: RuntimeError: net down" in out


# --------------------------------------------------------------------------- save tools


def test_archive_content(session, tmp_path, monkeypatch):
    from backend.services.agent.core import fs as fsmod
    from backend.services.agent.tools import save as savemod

    monkeypatch.setattr(fsmod, "AGENT_TMP_ROOT", tmp_path)
    monkeypatch.setattr(fsmod.AGENT_BACKEND, "_root_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(fsmod.AGENT_BACKEND, "_virtual_mode", True, raising=False)

    # 重新计算 real path
    import backend.services.agent.tools.save as tsave

    tools = {t.name: t for t in tsave.build_save_tools(session)}
    out = asyncio_run(tools["archive_content"].ainvoke({"data": "hello", "fmt": "txt"}))
    assert "/agent_saved/" in out


def test_archive_content_error(session):
    from backend.services.agent.tools import save as tsave

    tools = {t.name: t for t in tsave.build_save_tools(session)}
    out = asyncio_run(tools["archive_content"].ainvoke({"data": "x", "fmt": "bad"}))
    assert "保存失败" in out


# --------------------------------------------------------------------------- editor tools


def test_get_editor_code(session):
    from backend.services.agent.session.model import EditorState
    from backend.services.agent.tools.editor import build_editor_tools

    editor = EditorState()
    tools = {t.name: t for t in build_editor_tools(session, FakeBridge(), editor)}
    out = asyncio_run(tools["get_editor_code"].ainvoke({}))
    assert "编辑器当前为空" in out
    editor.set("print(1)")
    out2 = asyncio_run(tools["get_editor_code"].ainvoke({}))
    assert out2 == "print(1)"


def test_set_editor_code(session):
    from backend.services.agent.session.model import EditorState
    from backend.services.agent.tools.editor import build_editor_tools

    editor = EditorState()
    tools = {t.name: t for t in build_editor_tools(session, FakeBridge(), editor)}
    out = asyncio_run(tools["set_editor_code"].ainvoke({"code": "x = 1"}))
    assert "已写回编辑器" in out
    assert editor.get() == "x = 1"


def test_set_editor_code_no_editor(session):
    from backend.services.agent.tools.editor import build_editor_tools

    tools = {t.name: t for t in build_editor_tools(session, FakeBridge(), None)}
    out = asyncio_run(tools["set_editor_code"].ainvoke({"code": "x"}))
    assert "编辑器不可用" in out


def test_debug_code(session, tmp_path, monkeypatch):
    from backend.services.agent.core import fs as fsmod
    from backend.services.agent.tools.editor import build_editor_tools

    monkeypatch.setattr(fsmod, "AGENT_TMP_ROOT", tmp_path)
    monkeypatch.setattr(fsmod.AGENT_BACKEND, "_root_dir", str(tmp_path), raising=False)

    bridge = FakeBridge()
    bridge.run_result = {"ok": True, "output": "dbg-out", "error": "", "saved": []}
    tools = {t.name: t for t in build_editor_tools(session, bridge, None)}
    out = asyncio_run(tools["debug_code"].ainvoke({"code": "print(1)", "filename": "dbg.py"}))
    assert "临时脚本" in out
    assert "ok=True" in out


def test_debug_code_collapses_progress_frames(session, tmp_path, monkeypatch):
    from backend.services.agent.core import fs as fsmod
    from backend.services.agent.tools.editor import build_editor_tools

    monkeypatch.setattr(fsmod, "AGENT_TMP_ROOT", tmp_path)
    monkeypatch.setattr(fsmod.AGENT_BACKEND, "_root_dir", str(tmp_path), raising=False)

    bridge = FakeBridge()
    bridge.run_result = {
        "ok": True,
        "output": "\r翻页: 0/2\r翻页: 1/2\r翻页: 2/2\n完成\n",
        "error": "",
        "saved": [],
    }
    tools = {t.name: t for t in build_editor_tools(session, bridge, None)}
    out = asyncio_run(tools["debug_code"].ainvoke({"code": "print(1)"}))
    assert "翻页: 2/2" in out
    assert "翻页: 0/2" not in out
    assert "完成" in out


def test_debug_code_write_failure(session, tmp_path, monkeypatch):
    from backend.services.agent.core import fs as fsmod
    from backend.services.agent.tools.editor import build_editor_tools

    class BoomBackend:
        def _resolve_path(self, v):
            raise RuntimeError("boom")

    monkeypatch.setattr(fsmod, "AGENT_BACKEND", BoomBackend(), raising=False)
    tools = {t.name: t for t in build_editor_tools(session, FakeBridge(), None)}
    out = asyncio_run(tools["debug_code"].ainvoke({"code": "x"}))
    assert "临时脚本生成失败" in out


# --------------------------------------------------------------------------- planning tools


def test_record_plan(session):
    from backend.services.agent.tools.planning import build_planning_tools

    tools = {t.name: t for t in build_planning_tools(session)}
    out = asyncio_run(tools["record_plan"].ainvoke({
        "plan": '{"goal":"x","steps":["s1","s2"]}'}))
    assert "规划已记录" in out
    assert session.plan["steps"] == [
        {"content": "s1", "status": "pending"},
        {"content": "s2", "status": "pending"},
    ]


def test_record_plan_invalid_json(session):
    from backend.services.agent.tools.planning import build_planning_tools

    tools = {t.name: t for t in build_planning_tools(session)}
    out = asyncio_run(tools["record_plan"].ainvoke({"plan": "not-json"}))
    assert "规划已记录" in out
    assert session.plan == {"raw": "not-json"}
    out2 = asyncio_run(tools["record_plan"].ainvoke({"plan": "[1,2]"}))
    assert session.plan == {"raw": "[1, 2]"}


def test_ask_user(monkeypatch, session):
    from backend.services.agent.tools.planning import build_planning_tools
    import backend.services.agent.tools.planning as pmod

    interrupted = {}

    def fake_interrupt(payload):
        interrupted["payload"] = payload
        return {"choice": "a"}

    monkeypatch.setattr(pmod, "interrupt", fake_interrupt)
    tools = {t.name: t for t in build_planning_tools(session)}
    out = asyncio_run(tools["ask_user"].ainvoke({
        "questions": '[{"key":"q","title":"选择","type":"single","options":["a","b"]}]'}))
    assert "用户已确认" in out
    assert interrupted["payload"]["kind"] == "ask_user"
    # 回答事件广播给前端(用于渲染问题+答案卡片)
    answers_events = [e for e in session.hub._buffer if e.get("type") == "answer"]
    assert answers_events
    assert answers_events[-1]["answers"] == {"choice": "a"}
    assert answers_events[-1]["questions"][0]["key"] == "q"


def test_ask_user_invalid(monkeypatch, session):
    from backend.services.agent.tools.planning import build_planning_tools
    import backend.services.agent.tools.planning as pmod

    interrupted = {}

    def fake_interrupt(payload):
        interrupted["payload"] = payload
        return {}

    monkeypatch.setattr(pmod, "interrupt", fake_interrupt)
    tools = {t.name: t for t in build_planning_tools(session)}
    asyncio_run(tools["ask_user"].ainvoke({"questions": "not-json"}))
    assert interrupted["payload"]["questions"][0]["key"] == "q0"


# --------------------------------------------------------------------------- helper


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
