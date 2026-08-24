"""backend.services.agent.session (event / model / store) 测试。"""

from __future__ import annotations

import asyncio

import pytest
from mongomock_motor import AsyncMongoMockClient


# --------------------------------------------------------------------------- EventHub


def test_event_hub_emit_and_buffer():
    from backend.services.agent.session.event import EventHub

    hub = EventHub(maxlen=3)
    hub.emit({"type": "status", "content": "a"})
    hub.emit({"type": "status", "content": "b"})
    hub.emit({"type": "status", "content": "c"})
    hub.emit({"type": "delta", "content": "tmp"})  # 瞬时事件不入缓冲
    q = hub.subscribe()
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert len(events) == 3
    assert "delta" not in [e["type"] for e in events]


async def test_event_hub_subscribe_replay():
    from backend.services.agent.session.event import EventHub

    hub = EventHub()
    hub.emit({"type": "plan", "plan": {}})
    q = hub.subscribe()
    first = await q.get()
    assert first["type"] == "plan"


def test_event_hub_unsubscribe():
    from backend.services.agent.session.event import EventHub

    hub = EventHub()
    q = hub.subscribe()
    hub.unsubscribe(q)
    assert hub._subs == []
    hub.unsubscribe(q)  # 幂等


def test_event_hub_queue_full():
    from backend.services.agent.session.event import EventHub

    hub = EventHub(maxlen=5)
    q = asyncio.Queue(maxsize=2)
    hub._subs.append(q)
    # 填满队列
    hub.emit({"type": "delta", "content": "1"})
    hub.emit({"type": "delta", "content": "2"})
    # 非瞬时事件 -> 队列满则移除订阅者
    hub.emit({"type": "plan", "plan": {}})
    assert hub._subs == []


# --------------------------------------------------------------------------- model


def test_agent_session_emit():
    from backend.services.agent.session.event import EventHub
    from backend.services.agent.session.model import AgentSession

    hub = EventHub()
    session = AgentSession(id="s1", crawler_id="c", title="t", hub=hub)
    session.emit({"type": "status"})
    assert hub._buffer[-1]["session_id"] == "s1"
    assert hub._buffer[-1]["crawler_id"] == "c"


def test_agent_session_defaults():
    from backend.services.agent.session.event import EventHub
    from backend.services.agent.session.model import AgentSession

    hub = EventHub()
    session = AgentSession(id="s1", crawler_id="c", title="t", hub=hub)
    assert session.status == "idle"
    assert session.started is False
    assert session.title_manual is False
    assert session.plan is None


def test_editor_state():
    from backend.services.agent.session.model import EditorState

    e = EditorState()
    assert e.get() == ""
    e.set("code = 1")
    assert e.get() == "code = 1"
    e.set("v2")
    e.mark_turn()
    assert e.base_code == "v2"


# --------------------------------------------------------------------------- AgentStore


@pytest.fixture()
def mongo_client():
    client = AsyncMongoMockClient()
    yield client
    client.close()


@pytest.fixture()
def store(mongo_client, monkeypatch):
    from backend.services.agent.session.store import AgentStore

    monkeypatch.setattr("backend.services.agent.session.store.AsyncIOMotorClient",
                        lambda uri, **kw: mongo_client)
    return AgentStore("mongodb://fake", "crawler")


async def test_store_create_and_get(store):
    doc = await store.create_session("s1", "cid", "标题")
    assert doc["session_id"] == "s1"
    got = await store.get_session("s1", "cid")
    assert got["id"] == "s1"
    assert got["title"] == "标题"
    assert "_id" not in got
    assert await store.get_session("missing", "cid") is None


async def test_store_list_sessions(store):
    await store.create_session("s1", "cid", "a")
    await store.create_session("s2", "cid", "b")
    await store.create_session("s3", "other", "c")
    docs = await store.list_sessions("cid")
    assert len(docs) == 2
    assert docs[0]["id"] in ("s1", "s2")


async def test_store_update_session(store):
    await store.create_session("s1", "cid", "a")
    await store.update_session("s1", "cid", status="running", message_count=2)
    doc = await store.get_session("s1", "cid")
    assert doc["status"] == "running"
    assert doc["message_count"] == 2
    assert "updated_at" in doc


async def test_store_messages(store):
    await store.create_session("s1", "cid", "t")
    m = await store.add_message("s1", "cid", "user", "text", "hello")
    assert m["content"] == "hello"
    assert "id" in m
    await store.add_message("s1", "cid", "assistant", "text", "hi", meta={"x": 1})
    msgs = await store.list_messages("s1", "cid")
    assert len(msgs) == 2
    assert msgs[0]["id"] is not None
    assert msgs[1]["meta"] == {"x": 1}
    assert await store.count_messages("s1", "cid") == 2


async def test_store_delete_session(store):
    await store.create_session("s1", "cid", "t")
    await store.add_message("s1", "cid", "user", "text", "m")
    await store.delete_session("s1", "cid")
    assert await store.get_session("s1", "cid") is None
    assert await store.count_messages("s1", "cid") == 0


async def test_store_connect_failure(store, mongo_client, monkeypatch):
    monkeypatch.setattr("backend.services.agent.session.store.AsyncIOMotorClient",
                        lambda uri, **kw: (_ for _ in ()).throw(RuntimeError("no mongo")))
    with pytest.raises(RuntimeError):
        await store.create_session("s1", "cid", "t")
    # 冷却期内直接失败
    assert store._down_until > 0
    with pytest.raises(ConnectionError, match="冷却"):
        await store._connect()


def test_store_strip():
    from backend.services.agent.session.store import AgentStore

    doc = AgentStore._strip({"_id": "obj", "a": 1})
    assert doc["id"] == "obj"
    assert "_id" not in doc
    doc2 = AgentStore._strip_session({"_id": "obj", "session_id": "s1"})
    assert doc2["id"] == "s1"
    assert "_id" not in doc2
