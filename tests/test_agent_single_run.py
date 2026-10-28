"""单 Agent 运行锁 / 交互超时 / 事件序号 / 临时目录清理测试。"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from backend.config import Config
from backend.services.agent.runner import AgentBusyError, AgentManager
from backend.services.agent.session.event import EventHub
from backend.services.agent.session.model import AgentSession


class FakeStore:
    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = {}

    async def create_session(self, session_id, crawler_id, title):
        self.sessions[session_id] = {
            "session_id": session_id,
            "crawler_id": crawler_id,
            "title": title,
            "status": "idle",
            "message_count": 0,
            "last_message": "",
        }

    async def get_session(self, session_id, crawler_id):
        doc = self.sessions.get(session_id)
        return dict(doc) if doc and doc["crawler_id"] == crawler_id else None

    async def list_sessions(self, crawler_id, limit=200):
        return [dict(d) for d in self.sessions.values() if d["crawler_id"] == crawler_id][:limit]

    async def update_session(self, session_id, crawler_id, **fields):
        if session_id in self.sessions:
            self.sessions[session_id].update(fields)

    async def delete_session(self, session_id, crawler_id):
        self.sessions.pop(session_id, None)
        self.messages.pop(session_id, None)

    async def add_message(self, session_id, crawler_id, role, type_, content="", meta=None):
        self.messages.setdefault(session_id, []).append({"content": content})

    async def list_messages(self, session_id, crawler_id, limit=5000):
        return self.messages.get(session_id, [])

    async def count_messages(self, session_id, crawler_id):
        return len(self.messages.get(session_id, []))


class FakeAgent:
    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": []})

    def astream(self, *args, **kwargs):
        async def _gen():
            if False:  # pragma: no cover - 保持 async generator 形态
                yield None

        return _gen()


def make_manager() -> AgentManager:
    mgr = AgentManager()
    mgr.setup(Config(), None)
    mgr.store = FakeStore()
    return mgr


def add_session(mgr: AgentManager, sid: str = "s1") -> AgentSession:
    s = AgentSession(id=sid, crawler_id=mgr.default_crawler_id(), title="T", hub=mgr.hub)
    mgr.sessions[sid] = s
    return s


async def test_single_run_lock_rejects_new_session_and_message():
    mgr = make_manager()
    running = add_session(mgr, "s1")
    running.status = "running"
    await mgr._run_lock.acquire()
    mgr._active_session_id = running.id
    try:
        with pytest.raises(AgentBusyError):
            await mgr.create_session(None, "另一个")
        other = add_session(mgr, "s2")
        with pytest.raises(AgentBusyError):
            await mgr.send_message(None, other.id, "hi")
    finally:
        running.status = "idle"
        mgr._release_run(running)
    assert mgr.is_busy() is False


async def test_api_returns_409_while_busy():
    import httpx
    from conftest import make_test_app

    mgr = make_manager()
    running = add_session(mgr, "s1")
    await mgr._run_lock.acquire()
    mgr._active_session_id = running.id
    try:
        app = make_test_app(agent=mgr)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/agent/session", json={"title": "x"})
        assert resp.status_code == 409
    finally:
        running.status = "idle"
        mgr._release_run(running)


async def test_lock_released_after_turn():
    mgr = make_manager()
    s = add_session(mgr, "s1")
    s.agent = FakeAgent()
    s.config = {"configurable": {"thread_id": s.id}}
    await mgr.send_message(None, s.id, "hi")
    assert mgr.is_busy() is True
    await s.task_handle
    assert mgr.is_busy() is False
    assert mgr.active_session_id() is None
    # 释放后可以创建新会话
    s2 = await mgr.create_session(None, "下一个")
    assert s2.id in mgr.sessions


async def test_ask_user_question_survives_refresh_and_resumes():
    mgr = make_manager()
    s = add_session(mgr, "s1")
    await mgr.store.create_session(s.id, s.crawler_id, s.title)
    # 让会话进入真实的一轮: 用 FakeAgent 触发 ask_user 打断。
    s.agent = FakeAgent()
    s.config = {"configurable": {"thread_id": s.id}}
    payload = SimpleNamespace(
        value={"kind": "ask_user", "qid": "q1", "questions": [{"key": "k", "title": "选哪个"}]}
    )
    task = asyncio.create_task(mgr._handle_question(s, payload))
    for _ in range(50):
        if s.status == "waiting" and s.question:
            break
        await asyncio.sleep(0)

    # 模拟刷新/切换会话后重新拉取会话列表: 问题必须仍在。
    sessions = await mgr.list_sessions(None)
    doc = next(d for d in sessions if d["session_id"] == s.id)
    assert doc["status"] == "waiting"
    assert doc["question"]["qid"] == "q1"
    assert mgr.store.sessions[s.id]["question"]["qid"] == "q1"

    await mgr.answer(None, s.id, "q1", {"k": "A"})
    answers = await task
    assert answers == {"k": "A"}
    assert s.status == "running"
    assert s.question is None
    # 回答后会话文档中的问题被清空, 刷新后不会重复弹出。
    assert mgr.store.sessions[s.id]["question"] is None


async def test_recover_stale_sessions():
    mgr = make_manager()
    mgr.store.sessions["s1"] = {
        "session_id": "s1", "crawler_id": mgr.default_crawler_id(),
        "status": "running", "title": "a",
    }
    mgr.store.sessions["s2"] = {
        "session_id": "s2", "crawler_id": mgr.default_crawler_id(),
        "status": "waiting", "title": "b",
    }
    mgr.store.sessions["s3"] = {
        "session_id": "s3", "crawler_id": mgr.default_crawler_id(),
        "status": "done", "title": "c",
    }
    assert await mgr.recover_stale_sessions() == 2
    assert mgr.store.sessions["s1"]["status"] == "interrupted"
    assert mgr.store.sessions["s2"]["status"] == "interrupted"
    assert mgr.store.sessions["s3"]["status"] == "done"


def test_event_hub_seq_and_incremental_subscribe():
    hub = EventHub()
    hub.emit({"type": "status", "content": "a", "session_id": "s1"})
    hub.emit({"type": "status", "content": "b", "session_id": "s1"})
    hub.emit({"type": "status", "content": "c", "session_id": "s2"})
    assert hub.latest_seq("s1") == 2
    q = hub.subscribe("s1", since=1)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [e["content"] for e in events] == ["b"]
    assert events[0]["seq"] == 2


def test_sweep_temp_dirs(tmp_path, monkeypatch):
    from backend.services.tmp_cleanup import sweep_temp_dirs

    now = time.time()
    old = tmp_path / ".chrome-profile" / "profile-old"
    old.mkdir(parents=True)
    new = tmp_path / ".chrome-profile" / "profile-new"
    new.mkdir()
    saved = tmp_path / "tmp" / "saved" / "old.txt"
    saved.parent.mkdir(parents=True)
    saved.write_text("x", encoding="utf-8")
    os.utime(old, (now - 2 * 86400, now - 2 * 86400))
    os.utime(saved, (now - 2 * 86400, now - 2 * 86400))
    removed = sweep_temp_dirs(tmp_path, max_age=86400, now=now)
    assert removed == 2
    assert not old.exists()
    assert new.exists()
    assert not saved.exists()


def test_safe_builtins_allows_class():
    from backend.services.sandbox import safe_builtins

    env = {"__builtins__": safe_builtins(), "__name__": "__main__"}
    exec("class A:\n    pass\nassert A.__name__ == 'A'", env)


async def test_exec_async_keeps_triple_quoted_content():
    from backend.services.browser import BrowserStream

    env = {"__builtins__": {}, "out": []}
    awaits = (
        's = """\nhello\n"""\n'
        "out.append(s)\n"
    )
    await BrowserStream._exec_async(awaits, env)
    assert env["out"] == ["\nhello\n"]


async def test_exec_async_supports_legacy_top_level_return():
    from backend.services.browser import BrowserStream

    env = {"__builtins__": {}, "out": []}
    await BrowserStream._exec_async(
        "out.append(1)\nif out:\n    return\nout.append(2)\n", env
    )
    assert env["out"] == [1]
