"""backend.routers.control 测试: 状态/页面/导航/截图/运行/登录/格式化/存储等。"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from conftest import FakeAgentManager, FakeStream, make_test_app


@pytest.fixture()
async def client():
    app = make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_status(client):
    resp = await client.get("/api/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["xvfb"] is True
    assert body["chrome"] is True


async def test_pages(client):
    resp = await client.get("/api/v1/pages")
    assert resp.status_code == 200
    assert resp.json()[0]["url"] == "https://example.com"


async def test_navigate_ok(client):
    resp = await client.post("/api/v1/navigate", json={"url": "https://a.com"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Title"


async def test_navigate_browser_error(client):
    resp = await client.post("/api/v1/navigate", json={"url": "http://fail"})
    assert resp.status_code == 502
    assert "boom" in resp.json()["detail"]


async def test_screenshot_ok(client):
    resp = await client.post("/api/v1/screenshot")
    assert resp.status_code == 200
    assert resp.content.startswith(b"\x89PNG")


async def test_screenshot_browser_error():
    from backend.services.browser import BrowserError

    class _ErrStream(FakeStream):
        async def screenshot(self):
            raise BrowserError("截图失败: boom")

    app = make_test_app(stream=_ErrStream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/api/v1/screenshot")
        assert resp.status_code == 502
        assert "boom" in resp.json()["detail"]


async def _run_chunks(resp) -> list[dict]:
    """解析 /run 的 SSE 流式响应(data: <json> 事件, 空行分隔)。"""
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    assert resp.headers.get("cache-control") == "no-cache"
    chunks: list[dict] = []
    for block in resp.text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                chunks.append(json.loads(line[5:].strip()))
    return chunks


async def test_run_ok(client):
    app = make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/api/v1/run", json={"code": "print(1)"})
        chunks = await _run_chunks(resp)
    assert chunks[0]["type"] == "start"
    assert chunks[0]["run_id"]
    assert isinstance(chunks[0]["ts"], (int, float))
    stdout = "".join(ch.get("data", "") for ch in chunks if ch["type"] == "stdout")
    assert stdout == "progress\n"
    assert all(isinstance(ch["ts"], (int, float)) for ch in chunks if ch["type"] == "stdout")
    done = chunks[-1]
    assert done["type"] == "done"
    assert isinstance(done["ts"], (int, float))
    assert done["result"]["ok"] is True
    assert done["result"]["output"] == "done"


async def test_run_error(client):
    resp = await client.post("/api/v1/run", json={"code": "raise error"})
    chunks = await _run_chunks(resp)
    done = chunks[-1]
    assert done["type"] == "done"
    assert done["result"]["ok"] is False
    assert done["result"]["error"] == "boom"


async def test_run_stream_heartbeat(monkeypatch):
    """脚本挂起时持续发送心跳 chunk, 防止前端等待超时。"""
    import backend.routers.control as control_mod

    class _SlowStream(FakeStream):
        async def run_code(self, code, login_gate=None, restart=True, on_output=None):
            await asyncio.sleep(0.2)
            return {"ok": True, "output": "slow", "error": "", "saved": []}

    monkeypatch.setattr(control_mod, "_HEARTBEAT_INTERVAL", 0.05)
    app = make_test_app(stream=_SlowStream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/api/v1/run", json={"code": "x"})
        chunks = await _run_chunks(resp)
    assert any(ch["type"] == "heartbeat" for ch in chunks)
    assert chunks[-1]["type"] == "done"
    assert chunks[-1]["result"]["output"] == "slow"


async def test_run_blocking_stream_heartbeat(monkeypatch):
    """阻塞式脚本(time.sleep)在 worker 事件循环执行, 不拖垮主循环:
    实时 stdout + 空闲心跳 + done 结束标记都必须按时到达。"""
    import backend.routers.control as control_mod

    class _BlockingStream(FakeStream):
        async def run_code(self, code, login_gate=None, restart=True, on_output=None):
            for i in range(3):
                if on_output:
                    on_output(f"line{i}\n")
                time.sleep(0.1)  # 阻塞调用: 只能发生在 worker 事件循环里
            return {"ok": True, "output": "ok", "error": "", "saved": []}

    monkeypatch.setattr(control_mod, "_HEARTBEAT_INTERVAL", 0.02)
    app = make_test_app(stream=_BlockingStream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/api/v1/run", json={"code": "x"})
        chunks = await _run_chunks(resp)
    assert any(ch["type"] == "heartbeat" for ch in chunks)
    stdout = "".join(ch.get("data", "") for ch in chunks if ch["type"] == "stdout")
    assert stdout == "line0\nline1\nline2\n"
    assert chunks[-1]["type"] == "done"
    assert chunks[-1]["result"]["output"] == "ok"


async def test_run_drain_leftover_output():
    """运行结束时队列里仍有残留输出, done 前必须完整冲刷。"""
    class _MultiLineStream(FakeStream):
        async def run_code(self, code, login_gate=None, restart=True, on_output=None):
            if on_output:
                on_output("first\n")
                on_output("second\n")
                on_output("third\n")
            return {"ok": True, "output": "ok", "error": "", "saved": []}

    app = make_test_app(stream=_MultiLineStream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/api/v1/run", json={"code": "x"})
        chunks = await _run_chunks(resp)
    stdout = "".join(ch.get("data", "") for ch in chunks if ch["type"] == "stdout")
    assert stdout == "first\nsecond\nthird\n"
    assert chunks[-1]["type"] == "done"
    assert chunks[-1]["result"]["output"] == "ok"


async def test_run_worker_exception():
    """worker 线程抛异常时, 仍以 done 事件返回错误结果, 不中断流。"""
    class _RaiseStream(FakeStream):
        async def run_code(self, code, login_gate=None, restart=True, on_output=None):
            raise RuntimeError("boom-thread")

    app = make_test_app(stream=_RaiseStream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/api/v1/run", json={"code": "x"})
        chunks = await _run_chunks(resp)
    assert chunks[-1]["type"] == "done"
    assert chunks[-1]["result"]["ok"] is False
    assert "boom-thread" in chunks[-1]["result"]["error"]


async def test_run_login_status_not_waiting(client):
    resp = await client.get("/api/v1/run/abc/login")
    assert resp.status_code == 200
    body = resp.json()
    assert body["waiting"] is False
    assert body["request"] is None


async def test_run_login_answer_no_gate(client):
    resp = await client.post("/api/v1/run/abc/login-answer", json={"answers": {}})
    assert resp.status_code == 404


async def test_run_login_action_no_gate(client):
    resp = await client.post("/api/v1/run/abc/login-action", json={"action": "send_code"})
    assert resp.status_code == 404


async def test_format_black_ok(client):
    resp = await client.post("/api/v1/format", json={"code": "x=1\n"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["formatted"].startswith("x = 1")


async def test_format_black_nothing_changed(client):
    resp = await client.post("/api/v1/format", json={"code": "x = 1\n"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["formatted"] == "x = 1\n"


async def test_format_black_syntax_error(client):
    resp = await client.post("/api/v1/format", json={"code": "def (:\n"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


async def test_format_black_missing(client, monkeypatch):
    import builtins

    import backend.routers.control as control_mod

    real = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "black":
            raise ImportError("no black")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    resp = await client.post("/api/v1/format", json={"code": "x=1"})
    assert resp.json()["ok"] is False
    assert "black" in resp.json()["error"]


async def test_organize_imports_ok(client):
    resp = await client.post(
        "/api/v1/organize-imports", json={"code": "import os\nimport sys\n"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_organize_imports_missing(client, monkeypatch):
    import builtins

    real = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "isort":
            raise ImportError("no isort")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    resp = await client.post("/api/v1/organize-imports", json={"code": "import os\n"})
    assert resp.json()["ok"] is False
    assert "isort" in resp.json()["error"]


async def test_organize_imports_exception(client, monkeypatch):
    import builtins

    real = builtins.__import__

    class _Isort:
        @staticmethod
        def code(code):
            raise RuntimeError("boom")

    def fake_import(name, *args, **kwargs):
        if name == "isort":
            return _Isort()
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    resp = await client.post("/api/v1/organize-imports", json={"code": "import os\n"})
    assert resp.json()["ok"] is False
    assert "boom" in resp.json()["error"]


async def test_restart(client):
    resp = await client.post("/api/v1/restart")
    assert resp.status_code == 200
    assert resp.json()["xvfb"] is True


async def test_console_eval(client):
    resp = await client.post("/api/v1/console/eval", json={"expression": "1+1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_console_properties(client):
    resp = await client.post(
        "/api/v1/console/properties", json={"object_id": "obj1"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_network_body(client):
    resp = await client.post("/api/v1/network/body", json={"request_id": "r1"})
    assert resp.status_code == 200
    assert resp.json()["body"] == "text"


async def test_network_clear(client):
    resp = await client.post("/api/v1/network/clear")
    assert resp.status_code == 200


async def test_dom_tree(client):
    resp = await client.post("/api/v1/dom/tree")
    assert resp.status_code == 200
    assert resp.json()["root"]["id"] == 1


async def test_dom_box(client):
    resp = await client.post("/api/v1/dom/box", json={"backend_node_id": 5})
    assert resp.status_code == 200
    assert resp.json()["box"]["w"] == 10


async def test_storage_origin(client):
    resp = await client.post("/api/v1/storage/origin")
    assert resp.status_code == 200
    assert resp.json()["origin"] == "https://example.com"


async def test_storage_items(client):
    resp = await client.post(
        "/api/v1/storage/items", json={"origin": "https://example.com"}
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["key"] == "k"


async def test_storage_set(client):
    resp = await client.post(
        "/api/v1/storage/set",
        json={"origin": "https://example.com", "key": "k", "value": "v"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_storage_remove(client):
    resp = await client.post(
        "/api/v1/storage/remove",
        json={"origin": "https://example.com", "key": "k", "value": ""},
    )
    assert resp.status_code == 200


async def test_storage_cookies(client):
    resp = await client.post(
        "/api/v1/storage/cookies", json={"origin": "https://example.com"}
    )
    assert resp.status_code == 200


async def test_storage_cookie_set(client):
    resp = await client.post(
        "/api/v1/storage/cookie/set",
        json={"origin": "https://example.com", "name": "n", "value": "v"},
    )
    assert resp.status_code == 200


async def test_storage_cookie_delete(client):
    resp = await client.post(
        "/api/v1/storage/cookie/delete",
        json={"origin": "https://example.com", "name": "n"},
    )
    assert resp.status_code == 200


async def test_storage_idb_databases(client):
    resp = await client.post(
        "/api/v1/storage/idb/databases", json={"origin": "https://example.com"}
    )
    assert resp.status_code == 200
    assert resp.json()["databases"] == ["db1"]


async def test_storage_idb_stores(client):
    resp = await client.post(
        "/api/v1/storage/idb/stores",
        json={"origin": "https://example.com", "database": "db1"},
    )
    assert resp.status_code == 200


async def test_storage_idb_data(client):
    resp = await client.post(
        "/api/v1/storage/idb/data",
        json={"origin": "https://example.com", "database": "db1", "store": "s1"},
    )
    assert resp.status_code == 200
    assert resp.json()["has_more"] is False


async def test_storage_idb_data_pagination(client):
    resp = await client.post(
        "/api/v1/storage/idb/data",
        json={
            "origin": "https://example.com",
            "database": "db1",
            "store": "s1",
            "skip": 10,
            "count": 5,
        },
    )
    assert resp.status_code == 200


async def test_run_with_gate_roundtrip():
    """独立运行的登录协作: 新建 gate → 轮询 → 提交答案 → 移除。"""
    from backend.routers.control import _run_login
    from backend.services.agent.bridge import BrowserBridge
    from backend.services.agent.run_login import RunLoginManager
    from backend.services.agent.session.event import EventHub

    app = make_test_app()
    hub = EventHub()
    run_login = RunLoginManager(hub)
    app.state.run_login = run_login
    stream = app.state.stream
    bridge = BrowserBridge(stream)

    run_id = "testrun123"
    gate = run_login.new_gate(run_id, bridge)

    async def fake_payload(*a, **kw):
        return None

    gate._payload = {
        "qid": "q1",
        "login_type": "account",
        "method": "account",
        "url": "https://login",
        "message": "login",
        "timeout": 60,
        "fields": [],
        "captcha": {"type": "none"},
        "submit_label": "登录",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get(f"/api/v1/run/{run_id}/login")
        assert resp.status_code == 200
        body = resp.json()
        assert body["waiting"] is True
        assert body["request"]["qid"] == "q1"

        gate._payload = None
        resp = await c.get(f"/api/v1/run/{run_id}/login")
        assert resp.json()["waiting"] is False

        resp = await c.post(
            f"/api/v1/run/{run_id}/login-answer",
            json={"answers": {"cancelled": True}},
        )
        assert resp.status_code == 400  # gate 存在但 payload 失效 → ValueError


async def test_run_login_answer_success():
    import asyncio

    from backend.services.agent.bridge import BrowserBridge
    from backend.services.agent.run_login import RunLoginManager
    from backend.services.agent.session.event import EventHub

    app = make_test_app()
    run_login = RunLoginManager(EventHub())
    app.state.run_login = run_login
    run_id = "runansok"
    gate = run_login.new_gate(run_id, BrowserBridge(app.state.stream))
    gate._payload = {"qid": "q1"}
    gate._future = asyncio.get_running_loop().create_future()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            f"/api/v1/run/{run_id}/login-answer", json={"answers": {"a": 1}}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert gate._future.result() == {"a": 1}


async def test_run_login_answer_value_error():
    from backend.services.agent.run_login import RunLoginManager
    from backend.services.agent.session.event import EventHub

    app = make_test_app()
    hub = EventHub()
    run_login = RunLoginManager(hub)
    app.state.run_login = run_login
    stream = app.state.stream

    from backend.services.agent.bridge import BrowserBridge

    run_id = "runvalerr"
    gate = run_login.new_gate(run_id, BrowserBridge(stream))
    gate._future = None  # 让 answer 抛 ValueError

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            f"/api/v1/run/{run_id}/login-answer", json={"answers": {"x": 1}}
        )
        assert resp.status_code == 400


async def test_run_login_action_send_code_ok():
    from backend.services.agent.run_login import RunLoginManager
    from backend.services.agent.session.event import EventHub

    app = make_test_app()
    run_login = RunLoginManager(EventHub())
    app.state.run_login = run_login

    from backend.services.agent.bridge import BrowserBridge

    run_id = "runaction"
    gate = run_login.new_gate(run_id, BrowserBridge(app.state.stream))
    gate._payload = {"captcha": {"send_selector": "button"}}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            f"/api/v1/run/{run_id}/login-action", json={"action": "send_code"}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


async def test_run_login_action_refresh_captcha():
    from backend.services.agent.run_login import RunLoginManager
    from backend.services.agent.session.event import EventHub

    app = make_test_app()
    run_login = RunLoginManager(EventHub())
    app.state.run_login = run_login

    from backend.services.agent.bridge import BrowserBridge

    run_id = "runaction2"
    gate = run_login.new_gate(run_id, BrowserBridge(app.state.stream))
    gate._payload = {"captcha": {"refresh_selector": "img"}}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            f"/api/v1/run/{run_id}/login-action", json={"action": "refresh_captcha"}
        )
        assert resp.status_code == 200


async def test_run_login_action_refresh_qr():
    from backend.services.agent.run_login import RunLoginManager
    from backend.services.agent.session.event import EventHub

    app = make_test_app()
    run_login = RunLoginManager(EventHub())
    app.state.run_login = run_login

    from backend.services.agent.bridge import BrowserBridge

    run_id = "runqr"
    gate = run_login.new_gate(run_id, BrowserBridge(app.state.stream))
    gate._payload = {"url": "http://login"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            f"/api/v1/run/{run_id}/login-action", json={"action": "refresh_qr"}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


async def test_run_login_action_unknown():
    from backend.services.agent.run_login import RunLoginManager
    from backend.services.agent.session.event import EventHub

    app = make_test_app()
    run_login = RunLoginManager(EventHub())
    app.state.run_login = run_login

    from backend.services.agent.bridge import BrowserBridge

    run_id = "runactunk"
    gate = run_login.new_gate(run_id, BrowserBridge(app.state.stream))
    gate._payload = {}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            f"/api/v1/run/{run_id}/login-action", json={"action": "nope"}
        )
        assert resp.status_code == 400
