"""backend.routers.agent 测试: 会话管理 / 消息 / 问卷 / 停止 / 编辑器镜像。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from conftest import FakeAgentManager, make_test_app


@pytest.fixture()
async def client():
    app = make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_agent_info(client):
    resp = await client.get("/api/v1/agent/info")
    assert resp.status_code == 200
    assert resp.json()["crawler_id"] == "dev_test"


async def test_agent_info_with_cfg(client):
    from backend.config import Config

    cfg = Config(crawler_id="mycrawler")
    app = make_test_app(cfg=cfg)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/api/v1/agent/info")
        assert resp.json()["crawler_id"] == "mycrawler"


async def test_agent_session_create(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "任务"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "idle"
    assert body["crawler_id"] == "default"
    assert body["session_id"]


async def test_agent_session_create_empty_title(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "  "})
    assert resp.status_code == 400


async def test_agent_sessions_list(client):
    await client.post("/api/v1/agent/session", json={"title": "A"})
    resp = await client.get("/api/v1/agent/sessions")
    assert resp.status_code == 200
    assert len(resp.json()["sessions"]) == 1


async def test_agent_sessions_list_with_crawler_id(client):
    resp = await client.get("/api/v1/agent/sessions?crawler_id=other")
    assert resp.status_code == 200
    assert resp.json()["sessions"] == []


async def test_agent_session_messages(client):
    app = make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        created = (await c.post("/api/v1/agent/session", json={"title": "T"})).json()
        sid = created["session_id"]
        resp = await c.get(f"/api/v1/agent/session/{sid}/messages")
        assert resp.status_code == 200
        assert resp.json()["messages"] == []


async def test_agent_session_message_ok(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/api/v1/agent/session/{sid}/message", json={"content": "hi"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


async def test_agent_session_message_not_found(client):
    resp = await client.post(
        "/api/v1/agent/session/nope/message", json={"content": "hi"}
    )
    assert resp.status_code == 404


async def test_agent_session_message_empty(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/api/v1/agent/session/{sid}/message", json={"content": "   "}
    )
    assert resp.status_code == 400


async def test_agent_session_delete(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.delete(f"/api/v1/agent/session/{sid}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_agent_session_rename(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.patch(f"/api/v1/agent/session/{sid}", json={"title": "新标题"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "新标题"


async def test_agent_session_rename_not_found(client):
    resp = await client.patch("/api/v1/agent/session/nope", json={"title": "x"})
    assert resp.status_code == 404


async def test_agent_session_rename_empty(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.patch(f"/api/v1/agent/session/{sid}", json={"title": ""})
    assert resp.status_code == 400


async def test_agent_start(client):
    resp = await client.post("/api/v1/agent/start", json={"task": "采集"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"


async def test_agent_start_empty_task(client):
    resp = await client.post("/api/v1/agent/start", json={"task": ""})
    assert resp.status_code == 400


async def test_agent_start_generic_exception():
    from conftest import FakeAgentManager

    class _Mgr(FakeAgentManager):
        async def start(self, task):
            raise RuntimeError("LLM 配置错误")

    app = make_test_app(agent=_Mgr())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/api/v1/agent/start", json={"task": "x"})
        assert resp.status_code == 400
        assert "LLM" in resp.json()["detail"]


async def test_editor_code_get_set(client):
    resp = await client.get("/api/v1/editor/code")
    assert resp.json()["code"] == ""
    resp = await client.post("/api/v1/editor/code", json={"code": "print(1)"})
    assert resp.json()["ok"] is True
    resp = await client.get("/api/v1/editor/code")
    assert resp.json()["code"] == "print(1)"


async def test_agent_answer_ok(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.post(
        "/api/v1/agent/answer",
        json={"session_id": sid, "qid": "q1", "answers": {"a": 1}},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_agent_answer_not_found(client):
    resp = await client.post(
        "/api/v1/agent/answer",
        json={"session_id": "nope", "qid": "q1", "answers": {}},
    )
    assert resp.status_code == 404


async def test_agent_answer_value_error(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.post(
        "/api/v1/agent/answer",
        json={"session_id": sid, "qid": "", "answers": {}},
    )
    assert resp.status_code == 400


async def test_agent_session_finalize(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.post(f"/api/v1/agent/session/{sid}/finalize", json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_agent_stop(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.post("/api/v1/agent/stop", json={"session_id": sid})
    assert resp.status_code == 200


async def test_agent_stop_not_found(client):
    resp = await client.post("/api/v1/agent/stop", json={"session_id": "nope"})
    assert resp.status_code == 404


async def test_agent_login_action_ok(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.post(
        "/api/v1/agent/login-action",
        json={"session_id": sid, "action": "send_code"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_agent_login_action_refresh_qr(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.post(
        "/api/v1/agent/login-action",
        json={"session_id": sid, "action": "refresh_qr"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_agent_login_action_bad_action(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.post(
        "/api/v1/agent/login-action",
        json={"session_id": sid, "action": "nope"},
    )
    assert resp.status_code == 400


async def test_agent_login_action_not_found(client):
    resp = await client.post(
        "/api/v1/agent/login-action",
        json={"session_id": "nope", "action": "send_code"},
    )
    assert resp.status_code == 404


async def test_agent_login_answer_ok(client):
    resp = await client.post("/api/v1/agent/session", json={"title": "T"})
    sid = resp.json()["session_id"]
    resp = await client.post(
        "/api/v1/agent/login-answer",
        json={"session_id": sid, "qid": "q1", "answers": {"a": 1}},
    )
    assert resp.status_code == 200


async def test_agent_login_answer_not_found(client):
    resp = await client.post(
        "/api/v1/agent/login-answer",
        json={"session_id": "nope", "qid": "q1", "answers": {}},
    )
    assert resp.status_code == 404


async def test_agent_status(client):
    resp = await client.get("/api/v1/agent/status")
    assert resp.status_code == 200
    assert "sessions" in resp.json()


# --------------------------------------------------------------------------- WebSocket


async def test_ws_agent_hello_and_ping():
    from backend.routers.agent import ws_agent
    from conftest import FakeWS

    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_agent(ws))
    try:
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.05)
        assert ws.sent, "未收到 hello 事件"
        hello = json.loads(ws.sent[0])
        assert hello["type"] == "hello"
        assert hello["crawler_id"] == "default"
        assert hello["sessions"] == []
    finally:
        task.cancel()


async def test_ws_agent_session_filter_and_disconnect():
    from backend.routers.agent import ws_agent
    from conftest import FakeWS

    app = make_test_app()
    mgr = app.state.agent
    await mgr.create_session(None, "任务A")
    ws = FakeWS(app=app, query_params={"session": "s1"})
    task = asyncio.create_task(ws_agent(ws))
    try:
        await asyncio.sleep(0.2)
        mgr.hub.emit({"type": "delta", "content": "x", "session_id": "other"})
        mgr.hub.emit({"type": "delta", "content": "y", "session_id": "s1"})
        await asyncio.sleep(0.1)
        sent_types = [json.loads(m)["type"] for m in ws.sent]
        assert "delta" in sent_types
    finally:
        task.cancel()


async def test_ws_agent_ping_on_timeout(monkeypatch):
    import backend.routers.agent as agent_router
    from conftest import FakeWS

    orig_wait_for = asyncio.wait_for
    orig_sleep = asyncio.sleep

    async def fake_wait_for(awaitable, timeout):
        await orig_sleep(0)  # 让出事件循环, 避免饥饿
        try:
            await orig_wait_for(awaitable, 0.001)  # 真正 await, 避免弃用协程告警
        except asyncio.TimeoutError:
            pass
        raise asyncio.TimeoutError()

    monkeypatch.setattr(agent_router.asyncio, "wait_for", fake_wait_for)
    app = make_test_app()
    ws = FakeWS(app=app)
    task = asyncio.create_task(agent_router.ws_agent(ws))
    try:
        await asyncio.sleep(0.1)
        assert any(json.loads(m).get("type") == "ping" for m in ws.sent)
    finally:
        task.cancel()


async def test_ws_agent_send_failure_breaks():
    from fastapi import WebSocketDisconnect

    from backend.routers.agent import ws_agent
    from conftest import FakeWS

    app = make_test_app()
    mgr = app.state.agent

    class FailingWS(FakeWS):
        def __init__(self, app):
            super().__init__(app=app)
            self.calls = 0

        async def send_text(self, data):
            self.calls += 1
            if self.calls >= 2:
                raise WebSocketDisconnect()
            self.sent.append(data)

    ws = FailingWS(app)
    task = asyncio.create_task(ws_agent(ws))
    try:
        await asyncio.sleep(0.05)  # hello 先发出(call 1)
        mgr.hub.emit({"type": "delta", "content": "y"})  # 触发 send_text call 2
        await asyncio.sleep(0.1)
        assert task.done()  # 内层 send 失败 → break → 正常结束
    finally:
        task.cancel()


async def test_ws_agent_inner_exception():
    from backend.routers.agent import ws_agent
    from conftest import FakeWS

    class _Mgr(FakeAgentManager):
        async def list_sessions(self, crawler_id):
            raise RuntimeError("boom")

    app = make_test_app(agent=_Mgr())
    ws = FakeWS(app=app)
    task = asyncio.create_task(ws_agent(ws))
    try:
        await asyncio.sleep(0.1)
        assert task.done()  # 外层 except Exception 兜底
    finally:
        task.cancel()


async def test_agent_start_generic_error():
    from conftest import FakeAgentManager

    class _Mgr(FakeAgentManager):
        async def start(self, task):
            raise OSError("bad")

    app = make_test_app(agent=_Mgr())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/api/v1/agent/start", json={"task": "x"})
        assert resp.status_code == 400
        assert "Agent 启动失败" in resp.json()["detail"]


async def test_ws_agent_hello_send_failure():
    from fastapi import WebSocketDisconnect

    from backend.routers.agent import ws_agent
    from conftest import FakeWS

    app = make_test_app()

    class FailHelloWS(FakeWS):
        async def send_text(self, data):
            raise WebSocketDisconnect()

    ws = FailHelloWS(app)
    task = asyncio.create_task(ws_agent(ws))
    try:
        await asyncio.sleep(0.1)
        assert task.done()  # 外层 except WebSocketDisconnect 兜底
    finally:
        task.cancel()


async def test_agent_login_answer_value_error():
    from conftest import FakeAgentManager

    class _Mgr(FakeAgentManager):
        async def login_answer(self, crawler_id, session_id, qid, answers):
            raise ValueError("编号不匹配")

    app = make_test_app(agent=_Mgr())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/api/v1/agent/login-answer",
            json={"session_id": "s1", "qid": "q1", "answers": {}},
        )
        assert resp.status_code == 400
