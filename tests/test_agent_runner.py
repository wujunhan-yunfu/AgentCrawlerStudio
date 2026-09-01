"""backend.services.agent.runner.AgentManager 测试。

使用内存版 FakeStore / FakeAgent / FakeModel, 不依赖 MongoDB 与真实 LLM。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.types import Command

from conftest import FakeStream

# --------------------------------------------------------------------------- 假对象


class FakeStore:
    """AgentStore 的内存版, 行为一致但无 Mongo。"""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}

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
        d = self.sessions.get(session_id)
        if d is not None and d["crawler_id"] == crawler_id:
            return dict(d)
        return None

    async def list_sessions(self, crawler_id, limit=200):
        return [
            dict(d)
            for d in self.sessions.values()
            if d["crawler_id"] == crawler_id
        ][:limit]

    async def update_session(self, session_id, crawler_id, **fields):
        d = self.sessions.get(session_id)
        if d is not None:
            d.update(fields)

    async def delete_session(self, session_id, crawler_id):
        self.sessions.pop(session_id, None)
        self.messages.pop(session_id, None)

    async def add_message(self, session_id, crawler_id, role, type_, content="", meta=None):
        self.messages.setdefault(session_id, []).append(
            {
                "session_id": session_id,
                "crawler_id": crawler_id,
                "role": role,
                "type": type_,
                "content": content,
                "meta": meta or {},
            }
        )

    async def list_messages(self, session_id, crawler_id, limit=5000):
        return [
            dict(m)
            for m in self.messages.get(session_id, [])
            if m["crawler_id"] == crawler_id
        ][:limit]

    async def count_messages(self, session_id, crawler_id):
        return len(
            [m for m in self.messages.get(session_id, []) if m["crawler_id"] == crawler_id]
        )


class FakeAgent:
    """最小 Agent 假对象: 按调用次数返回预置的 astream 阶段。"""

    def __init__(self, phases=None, state_messages=None):
        self.phases = phases or [[]]
        self.call = 0
        self.state_messages = state_messages or []
        self.state_calls = 0

    async def aget_state(self, config):
        self.state_calls += 1
        return SimpleNamespace(values={"messages": self.state_messages})

    def astream(self, *args, **kwargs):
        idx = min(self.call, len(self.phases) - 1)
        self.call += 1
        seq = self.phases[idx]

        async def _gen():
            for item in seq:
                yield item

        return _gen()


class FakeModel:
    def __init__(self, content="测试标题"):
        self._content = content

    async def ainvoke(self, messages):
        return SimpleNamespace(content=self._content)


def make_manager(store=None, stream=None):
    from backend.config import Config
    from backend.services.agent.runner import AgentManager

    mgr = AgentManager()
    mgr.setup(Config(), stream or FakeStream())
    if store is not None:
        mgr.store = store
    return mgr


@pytest.fixture()
def manager():
    return make_manager(store=FakeStore())


def make_session(manager, sid="s1", title="T"):
    """创建并注册一个内存会话(使用与 manager 解析一致的 crawler_id)。"""
    from backend.services.agent.session.model import AgentSession

    cid = manager.default_crawler_id()
    s = AgentSession(id=sid, crawler_id=cid, title=title, hub=manager.hub)
    manager.sessions[sid] = s
    return s


@pytest.fixture(autouse=True)
def _patch_agent_build(monkeypatch):
    """默认替换 LLM/Agent 构建, 避免 create_session 后台预构建触发真实依赖。"""
    from backend.services.agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(),
    )


# --------------------------------------------------------------------------- 基础


def test_init_and_default_crawler_id(manager):
    assert manager.default_crawler_id() == "dev_test"
    assert manager._resolve_crawler_id(None) == "dev_test"
    assert manager._resolve_crawler_id("other") == "other"


def test_resolve_crawler_id_no_cfg():
    from backend.services.agent.runner import AgentManager

    mgr = AgentManager()
    assert mgr._resolve_crawler_id(None) == "default"
    assert mgr._resolve_crawler_id("x") == "x"


async def test_store_call_default_when_no_store():
    from backend.services.agent.runner import AgentManager

    mgr = AgentManager()
    assert await mgr._store_call("whatever", default="fallback") == "fallback"


async def test_store_call_graceful_on_error(manager):
    class BoomStore(FakeStore):
        async def create_session(self, *a, **k):
            raise RuntimeError("mongo down")

    manager.store = BoomStore()
    assert await manager._store_call("create_session", "1", "c", "t", default=None) is None


# --------------------------------------------------------------------------- create_session


async def test_create_session_empty_title(manager):
    with pytest.raises(ValueError):
        await manager.create_session(None, "  ")


async def test_create_session_ok(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    built = []

    def fake_build_agent(cfg, session, bridge, editor=None):
        built.append(session)
        return FakeAgent()

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(runner_mod, "build_agent", fake_build_agent)

    session = await manager.create_session(None, "任务A")
    assert session.id in manager.sessions
    assert session.crawler_id == "dev_test"
    assert session.title == "任务A"
    await session.persist_task
    assert manager.store.sessions[session.id]["title"] == "任务A"
    # 预构建 agent 后台任务
    if session.build_task is not None:
        await session.build_task
    assert session.agent is not None
    assert built


async def test_create_session_with_crawler_id(manager):
    session = await manager.create_session("mycrawler", "T")
    assert session.crawler_id == "mycrawler"


async def test_create_session_prebuild_error(manager, monkeypatch):
    """create_session 后台预构建 to_thread 同步抛错时静默降级。"""
    from backend.services.agent import runner as runner_mod

    def boom(func, *args, **kwargs):
        raise RuntimeError("to_thread boom")

    monkeypatch.setattr(runner_mod.asyncio, "to_thread", boom)
    session = await manager.create_session(None, "T")
    assert session.build_task is None


async def test_evict_old(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(),
    )
    runner_mod._MAX_SESSIONS = 3
    try:
        for i in range(5):
            s = await manager.create_session(None, f"T{i}")
            if s.build_task:
                await s.build_task
            await s.persist_task
        assert len(manager.sessions) <= 3
    finally:
        runner_mod._MAX_SESSIONS = 200


# --------------------------------------------------------------------------- start / send_message


async def test_start_empty_task(manager):
    with pytest.raises(ValueError):
        await manager.start("  ")


async def test_start_no_cfg():
    from backend.services.agent.runner import AgentManager

    mgr = AgentManager()
    with pytest.raises(RuntimeError):
        await mgr.start("任务")


async def test_start_ok(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(
            phases=[[("custom", {"type": "plan", "plan": {"steps": []}})]]
        ),
    )
    session = await manager.start("采集数据")
    assert session.id in manager.sessions
    await session.task_handle
    assert manager.store.messages[session.id]
    assert session.status == "done"


async def test_send_message_session_not_found(manager):
    with pytest.raises(KeyError):
        await manager.send_message(None, "nope", "hi")


async def test_send_message_loading_from_store(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod
    from backend.services.agent.session.model import AgentSession

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(phases=[[("updates", {"n": {"todos": []}})], [("updates", {"n": {"todos": []}})], []]),
    )
    # 先落库一个会话
    s0 = await manager.create_session(None, "已存")
    await s0.persist_task
    sid = s0.id
    manager.sessions.pop(sid)  # 模拟后端重启后不在内存
    session = await manager.send_message(None, sid, "继续")
    assert session.id == sid
    await session.task_handle
    assert session.status in ("done", "error")


async def test_send_message_running_rejected(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(),
    )
    s = await manager.create_session(None, "T")
    if s.build_task:
        await s.build_task
    s.status = "running"
    with pytest.raises(ValueError):
        await manager.send_message(None, s.id, "hi")


async def test_send_message_empty_content(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(),
    )
    s = await manager.create_session(None, "T")
    with pytest.raises(ValueError):
        await manager.send_message(None, s.id, "   ")


async def test_auto_title_generated(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel("抓取商品列表"))
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(phases=[[("custom", {"type": "status", "content": "思考中..."})]]),
    )
    s = await manager.create_session(None, "占位标题")
    if s.build_task:
        await s.build_task
    await manager.send_message(None, s.id, "帮我抓取商品列表")
    await asyncio.sleep(0.1)  # 等待后台 auto_title
    assert s.title != "占位标题"
    if s.task_handle:
        await s.task_handle


async def test_auto_title_fallback(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    async def bad_ainvoke(self, messages):
        raise RuntimeError("llm down")

    FakeModel.ainvoke = bad_ainvoke
    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(),
    )
    s = await manager.create_session(None, "占位")
    if s.build_task:
        await s.build_task
    title = await manager._generate_title("第一行很长的任务描述\n第二行")
    assert title == "第一行很长的任务描述"
    assert manager._fallback_title("abc") == "abc"
    assert manager._fallback_title("") == ""
    await s.task_handle if s.task_handle else None


# --------------------------------------------------------------------------- delete / rename / list


async def test_delete_session(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(),
    )

    class FakeCp:
        async def adelete_thread(self, thread_id):
            self.deleted = thread_id

    cp = FakeCp()
    monkeypatch.setattr(runner_mod, "get_checkpointer", lambda cfg: cp)
    s = await manager.create_session(None, "T")
    await s.persist_task
    s.status = "running"
    loop = asyncio.get_running_loop()
    s.answer_future = loop.create_future()
    s.login_future = loop.create_future()
    s.task_handle = loop.create_task(asyncio.sleep(10))
    await manager.delete_session(None, s.id)
    assert s.id not in manager.sessions
    assert cp.deleted == s.id
    assert s.answer_future.cancelled()
    with pytest.raises(asyncio.CancelledError):
        await s.task_handle
    assert s.task_handle.cancelled()


async def test_delete_session_absent(manager):
    await manager.delete_session(None, "ghost")  # 不报错


async def test_rename_session(manager):
    s = await manager.create_session(None, "旧名")
    await s.persist_task
    out = await manager.rename_session(None, s.id, "新名")
    assert out.title == "新名"
    assert s.title_manual is True
    assert manager.store.sessions[s.id]["title"] == "新名"


async def test_rename_session_empty(manager):
    s = await manager.create_session(None, "旧名")
    with pytest.raises(ValueError):
        await manager.rename_session(None, s.id, "")


async def test_rename_session_not_found(manager):
    with pytest.raises(KeyError):
        await manager.rename_session(None, "ghost", "x")


async def test_rename_session_from_store(manager):
    s = await manager.create_session(None, "旧名")
    await s.persist_task
    manager.sessions.pop(s.id)
    out = await manager.rename_session(None, s.id, "新名")
    assert out.title == "新名"
    assert out.title_manual is True


async def test_list_and_get_messages(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(phases=[[("custom", {"type": "plan", "plan": {"steps": []}})], []]),
    )
    s = await manager.create_session(None, "T")
    await s.persist_task
    await manager.send_message(None, s.id, "hi")
    await s.task_handle
    lst = await manager.list_sessions(None)
    assert any(d["session_id"] == s.id for d in lst)
    msgs = await manager.get_messages(None, s.id)
    assert msgs
    assert await manager.get_messages(None, "ghost") == []


# --------------------------------------------------------------------------- answer / login / stop


async def test_answer_success(manager):
    s = await manager.create_session(None, "T")
    s.status = "waiting"
    s.answer_future = asyncio.get_running_loop().create_future()
    s.question = {"qid": "q1", "questions": []}
    await manager.answer(None, s.id, "q1", {"a": 1})
    assert s.answer_future.result() == {"a": 1}


async def test_answer_wrong_session(manager):
    with pytest.raises(KeyError):
        await manager.answer(None, "ghost", "q1", {})


async def test_answer_not_waiting(manager):
    s = await manager.create_session(None, "T")
    with pytest.raises(ValueError):
        await manager.answer(None, s.id, "q1", {})


async def test_answer_wrong_qid(manager):
    s = await manager.create_session(None, "T")
    s.status = "waiting"
    s.answer_future = asyncio.get_running_loop().create_future()
    s.question = {"qid": "q1"}
    with pytest.raises(ValueError):
        await manager.answer(None, s.id, "q2", {})


async def test_login_action_send_code(manager):
    s = await manager.create_session(None, "T")
    s.status = "waiting"
    s.login = {"captcha": {"send_selector": "#btn"}}
    result = await manager.login_action(None, s.id, "send_code")
    assert result["ok"] is True


async def test_login_action_refresh_captcha(manager):
    s = await manager.create_session(None, "T")
    s.status = "waiting"
    s.login = {"captcha": {"image_selector": "img"}}
    result = await manager.login_action(None, s.id, "refresh_captcha")
    assert result["ok"] is True
    assert result["image"]


async def test_login_action_refresh_qr(manager):
    s = await manager.create_session(None, "T")
    s.status = "waiting"
    s.login = {"url": "http://login"}
    result = await manager.login_action(None, s.id, "refresh_qr")
    assert result["ok"] is True
    assert "已刷新" in result["message"]


async def test_login_action_no_login(manager):
    s = await manager.create_session(None, "T")
    with pytest.raises(ValueError):
        await manager.login_action(None, s.id, "send_code")


async def test_login_action_unknown(manager):
    s = await manager.create_session(None, "T")
    s.status = "waiting"
    s.login = {"captcha": {}}
    with pytest.raises(ValueError):
        await manager.login_action(None, s.id, "nope")


async def test_login_action_wrong_session(manager):
    with pytest.raises(KeyError):
        await manager.login_action(None, "ghost", "send_code")


async def test_login_answer_success(manager):
    s = await manager.create_session(None, "T")
    s.status = "waiting"
    s.login_future = asyncio.get_running_loop().create_future()
    s.login = {"qid": "q1"}
    await manager.login_answer(None, s.id, "q1", {"a": 1})
    assert s.login_future.result() == {"a": 1}


async def test_login_answer_wrong_qid(manager):
    s = await manager.create_session(None, "T")
    s.status = "waiting"
    s.login_future = asyncio.get_running_loop().create_future()
    s.login = {"qid": "q1"}
    with pytest.raises(ValueError):
        await manager.login_answer(None, s.id, "q2", {})


async def test_login_answer_wrong_session(manager):
    with pytest.raises(KeyError):
        await manager.login_answer(None, "ghost", "q1", {})


async def test_login_answer_login_none(manager):
    s = await manager.create_session(None, "T")
    s.status = "waiting"
    s.login_future = asyncio.get_running_loop().create_future()
    s.login = None
    with pytest.raises(ValueError):
        await manager.login_answer(None, s.id, "q1", {})


async def test_login_answer_not_waiting(manager):
    s = await manager.create_session(None, "T")
    with pytest.raises(ValueError):
        await manager.login_answer(None, s.id, "q1", {})


async def test_stop_running(manager):
    s = await manager.create_session(None, "T")
    s.status = "running"
    loop = asyncio.get_running_loop()
    s.answer_future = loop.create_future()
    s.login_future = loop.create_future()
    started = loop.create_future()

    async def fake_turn():
        try:
            started.set_result(True)
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            s.status = "cancelled"
            raise

    s.task_handle = loop.create_task(fake_turn())
    await started
    await manager.stop(None, s.id)
    assert s.answer_future.cancelled()
    assert s.login_future.cancelled()
    assert manager.store.sessions[s.id]["status"] == "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await s.task_handle
    assert s.status == "cancelled"  # 任务自身的 CancelledError 处理器更新状态


async def test_stop_not_running(manager):
    s = await manager.create_session(None, "T")
    await manager.stop(None, s.id)  # idle → 直接返回


async def test_stop_wrong_session(manager):
    with pytest.raises(KeyError):
        await manager.stop(None, "ghost")


async def test_finalize_session(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: FakeModel())
    monkeypatch.setattr(
        runner_mod,
        "build_agent",
        lambda cfg, session, bridge, editor=None: FakeAgent(phases=[[("custom", {"type": "status", "content": "x"})], []]),
    )
    s = await manager.create_session(None, "T")
    await s.persist_task
    await manager.send_message(None, s.id, "hi")
    await s.task_handle
    out = await manager.finalize_session(None, s.id)
    assert out["ok"] is True
    assert out["status"] == "done"
    # 会话存在时同步内存状态
    s.status = "error"
    out = await manager.finalize_session(None, s.id, status="cancelled")
    assert s.status == "cancelled"
    assert out["status"] == "cancelled"


# --------------------------------------------------------------------------- editor


async def test_editor_code(manager):
    assert manager.editor_code() == ""
    manager.set_editor_code("print(1)")
    assert manager.editor_code() == "print(1)"


# --------------------------------------------------------------------------- 主运行循环


async def test_send_message_awaits_build_task(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sx", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    loop = asyncio.get_running_loop()
    s.build_task = loop.create_task(asyncio.sleep(0.05))
    manager.sessions[s.id] = s
    session = await manager.send_message(None, s.id, "hi")  # await build_task → agent 仍 None → 现场构建
    assert session.agent is not None
    if session.task_handle:
        await session.task_handle


async def test_send_message_agent_build_error(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    def boom_build(cfg, session, bridge, editor=None):
        raise RuntimeError("LLM key 缺失")

    monkeypatch.setattr(runner_mod, "build_agent", boom_build)
    s = await manager.create_session(None, "T")
    with pytest.raises(ValueError):
        await manager.send_message(None, s.id, "hi")


async def test_persist_turn_start_waits(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sy", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    loop = asyncio.get_running_loop()
    s.persist_task = loop.create_task(asyncio.sleep(0.05))
    await manager._persist_turn_start(s, "hi")
    assert manager.store.sessions.get(s.id) is None  # persist_task 是 sleep, 不落库


async def test_auto_title_skip_manual(manager):
    s = await manager.create_session(None, "占位")
    s.title_manual = True
    await manager._auto_title(s, "内容")  # 手动改名后跳过
    assert s.title == "占位"
    await manager._auto_title(s, "")  # 空标题跳过
    assert s.title == "占位"


async def test_auto_title_generate_error_fallback(manager, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(manager, "_generate_title", boom)
    s = await manager.create_session(None, "占位")
    s.title_manual = False
    await manager._auto_title(s, "第一行任务\n第二行")
    assert s.title == "第一行任务"  # 回退到首行截断


async def test_generate_title_list_content(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    class _ListModel:
        async def ainvoke(self, messages):
            return SimpleNamespace(content=[{"text": "  抓取商品  "}, {"text": "列表"}])

    monkeypatch.setattr(runner_mod, "build_chat_model", lambda cfg: _ListModel())
    assert await manager._generate_title("帮我抓取商品列表") == "抓取商品  列表"


async def test_persist_turn_start_persist_error(manager):
    from backend.services.agent.session.model import AgentSession

    async def boom(*a, **k):
        raise RuntimeError("boom")

    s = AgentSession(id="spv", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    loop = asyncio.get_running_loop()
    s.persist_task = loop.create_task(boom())
    await manager._persist_turn_start(s, "hi")  # persist_task 异常被吞掉
    assert manager.store.sessions.get(s.id) is None


async def test_generate_title_build_error(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    def boom(cfg):
        raise RuntimeError("no model")

    monkeypatch.setattr(runner_mod, "build_chat_model", boom)
    assert await manager._generate_title("内容") == "内容"


async def test_ensure_agent(manager, monkeypatch):
    from backend.services.agent import runner as runner_mod

    captured = {}

    def fake_build(cfg, session, bridge, editor=None):
        captured["session"] = session
        return FakeAgent()

    monkeypatch.setattr(runner_mod, "build_agent", fake_build)
    s = await manager.create_session(None, "T")
    manager._ensure_agent(s)
    assert s.agent is not None
    assert s.config == {"configurable": {"thread_id": s.id}}
    assert captured["session"] is s


async def test_load_history(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sz", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    manager.sessions[s.id] = s
    await manager._store_call(
        "add_message", s.id, s.crawler_id, "user", "text", "第一条"
    )
    await manager._store_call(
        "add_message", s.id, s.crawler_id, "assistant", "text", "回复"
    )
    await manager._store_call(
        "add_message", s.id, s.crawler_id, "event", "tool", "args", {"id": 1}
    )
    history = await manager._load_history(s)
    assert isinstance(history[0], HumanMessage)
    assert isinstance(history[1], AIMessage)
    assert len(history) == 2  # event 消息不注入


async def test_thread_has_history(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sw", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    s.agent = FakeAgent(state_messages=[HumanMessage(content="旧")])
    s.config = {"configurable": {"thread_id": s.id}}
    assert await manager._thread_has_history(s) is True
    s.agent = FakeAgent(state_messages=[])
    assert await manager._thread_has_history(s) is False
    s.agent = None
    assert await manager._thread_has_history(s) is False


async def test_run_turn_success_with_plan(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sr", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    s.agent = FakeAgent(
        phases=[
            [
                ("custom", {"type": "plan", "plan": {"goal": "g", "steps": []}}),
                ("messages", (AIMessageChunk(content="好的"), None)),
                ("updates", {"agent": {"todos": [{"content": "x", "status": "completed"}]}}),
            ],
            [("updates", {"agent": {"todos": []}})],
        ],
        state_messages=[AIMessage(content="最终结果", tool_calls=[])],
    )
    s.config = {"configurable": {"thread_id": s.id}}
    await manager._run_turn(s, "帮我抓取")
    assert s.status == "done"
    assert s.started is True
    # 第二轮: started=True 走 else 分支
    await manager._run_turn(s, "再来一轮")
    assert s.status == "done"


class _RaiseAgent(FakeAgent):
    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    def astream(self, *args, **kwargs):
        async def _gen():
            yield ("updates", {})
            raise self._exc

        return _gen()


async def test_run_turn_cancelled(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sc", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    manager.sessions[s.id] = s
    await manager._store_call("create_session", s.id, s.crawler_id, s.title)
    s.agent = _RaiseAgent(asyncio.CancelledError())
    s.config = {"configurable": {"thread_id": s.id}}
    await manager._run_turn(s, "hi")
    assert s.status == "cancelled"
    assert manager.store.sessions[s.id]["status"] == "cancelled"


async def test_run_turn_error(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="se", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    manager.sessions[s.id] = s
    await manager._store_call("create_session", s.id, s.crawler_id, s.title)
    s.agent = _RaiseAgent(RuntimeError("boom"))
    s.config = {"configurable": {"thread_id": s.id}}
    await manager._run_turn(s, "hi")
    assert s.status == "error"
    assert "boom" in s.error
    assert manager.store.sessions[s.id]["status"] == "error"


async def test_consume_interrupt_ask_user(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sq", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)

    class _IAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def astream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                seq = [
                    (
                        "updates",
                        {
                            "__interrupt__": [
                                SimpleNamespace(
                                    value={
                                        "kind": "ask_user",
                                        "qid": "q1",
                                        "questions": [{"key": "site"}],
                                    }
                                )
                            ]
                        },
                    )
                ]
            else:
                seq = [("custom", {"type": "status", "content": "resumed"})]
            async def _gen():
                for item in seq:
                    yield item

            return _gen()

    s.agent = _IAgent()
    s.config = {"configurable": {"thread_id": s.id}}
    loop = asyncio.get_running_loop()
    task = loop.create_task(manager._run_turn(s, "hi"))
    for _ in range(100):
        if s.answer_future is not None:
            break
        await asyncio.sleep(0.01)
    assert s.status == "waiting"
    assert s.question["qid"] == "q1"
    s.answer_future.set_result({"site": "a.com"})
    await task
    assert s.status == "done"


async def test_consume_unexpected_interrupt(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="su", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)

    class _IAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def astream(self, *args, **kwargs):
            self.calls += 1
            seq = [
                ("updates", {"__interrupt__": [SimpleNamespace(value={"kind": "other"})]}),
            ] if self.calls == 1 else [("custom", {"type": "status", "content": "done"})]
            async def _gen():
                for item in seq:
                    yield item

            return _gen()

    s.agent = _IAgent()
    s.config = {"configurable": {"thread_id": s.id}}
    await manager._run_turn(s, "hi")
    assert s.status == "done"


async def test_handle_event_custom_and_messages(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sh", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    await manager._handle_event(s, "custom", {"type": "tool", "name": "x", "args": "{}", "id": "1"})
    await manager._handle_event(s, "custom", "not-a-dict")
    await manager._handle_event(
        s, "messages", (AIMessageChunk(content="增量"), None)
    )
    # 带 tool_call_chunks 的 chunk 不转发 delta
    from langchain_core.messages.tool import ToolCallChunk

    chunk = AIMessageChunk(
        content="", tool_call_chunks=[ToolCallChunk(name="t", args="{}", id="1")]
    )
    await manager._handle_event(s, "messages", (chunk, None))
    await manager._handle_event(s, "messages", ("bad", None))
    await manager._handle_event(s, "messages", None)
    # updates 带 todos
    s.plan = {"steps": []}
    await manager._handle_event(
        s, "updates", {"agent": {"todos": [{"content": "t1", "status": "completed"}]}}
    )
    assert s.plan["steps"]  # 被 _sync_plan_status 更新


async def test_sync_plan_status(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sp", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    # 非 dict plan
    s.plan = "not-dict"
    assert manager._sync_plan_status(s, []) is None
    # steps 非 list
    s.plan = {"steps": "nope"}
    assert manager._sync_plan_status(s, []) is None
    # 正常匹配 + 新增
    s.plan = {"steps": [{"content": "a", "status": "pending"}, "b"]}
    updated = manager._sync_plan_status(
        s, [{"content": "a", "status": "completed"}, {"content": "c", "status": "pending"}]
    )
    assert updated["steps"][0]["status"] == "completed"
    assert updated["steps"][1]["status"] == "pending"
    assert updated["steps"][2]["content"] == "c"
    assert s.plan is updated
    # todos 空
    assert manager._sync_plan_status(s, []) is None


async def test_persist_event_branches(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sp2", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)

    async def _persist(event):
        await manager._persist_event(s, event)

    # 瞬时状态不落库
    await _persist({"type": "status", "content": "思考中..."})
    assert manager.store.messages.get(s.id) is None
    await _persist({"type": "unknown_type"})
    assert manager.store.messages.get(s.id) is None

    await _persist({"type": "status", "content": "开始"})
    await _persist({"type": "error", "content": "出错"})
    await _persist({"type": "plan", "plan": {"goal": "g"}})
    await _persist({"type": "todos", "todos": [{"content": "a"}]})
    await _persist({"type": "saved", "saved": [{"id": "1"}]})
    await _persist({"type": "question", "qid": "q1", "questions": []})
    await _persist({"type": "tool", "id": "t1", "name": "n", "args": "{}"})
    await _persist({"type": "tool_result", "id": "t1", "name": "n", "content": "c", "error": ""})
    await _persist({"type": "editor_code", "code": "print(1)", "base": ""})
    await _persist({"type": "login_request", "login": {"qid": "q"}})
    await _persist({"type": "login_success", "method": "qr", "url": "https://a"})
    await _persist({"type": "login_action", "action": "send_code", "ok": True, "message": "m"})
    msgs = await manager.get_messages(None, s.id)
    kinds = [m["type"] for m in msgs]
    assert "status" in kinds and "error" in kinds
    assert "plan" in kinds and "todos" in kinds
    assert "saved" in kinds and "question" in kinds
    assert "tool" in kinds and "tool_result" in kinds
    assert "editor_code" in kinds
    assert "login_request" in kinds and "login_success" in kinds and "login_action" in kinds


async def test_finalize_done(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sf", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    manager.sessions[s.id] = s
    await manager._store_call("create_session", s.id, s.crawler_id, s.title)
    s.agent = FakeAgent(state_messages=[AIMessage(content="汇总结果", tool_calls=[])])
    s.config = {"configurable": {"thread_id": s.id}}
    await manager._finalize_done(s)
    assert s.status == "done"
    msgs = await manager.get_messages(None, s.id)
    assert msgs and msgs[-1]["role"] == "assistant"
    assert manager.store.sessions[s.id]["last_message"] == "汇总结果"

    # aget_state 异常时降级
    s2 = AgentSession(id="sf2", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)

    class _BadStateAgent(FakeAgent):
        def aget_state(self, config):
            raise RuntimeError("boom")

    s2.agent = _BadStateAgent()
    s2.config = {"configurable": {"thread_id": s2.id}}
    await manager._finalize_done(s2)
    assert s2.status == "done"


async def test_finalize_done_empty_state(manager):
    from backend.services.agent.session.model import AgentSession

    s = AgentSession(id="sfe", crawler_id=manager.default_crawler_id(), title="T", hub=manager.hub)
    manager.sessions[s.id] = s
    await manager._store_call("create_session", s.id, s.crawler_id, s.title)
    s.agent = FakeAgent(state_messages=[])
    s.config = {"configurable": {"thread_id": s.id}}
    await manager._finalize_done(s)
    assert s.status == "done"
    assert manager.store.sessions[s.id]["last_message"] == "T"  # 无结果回退标题