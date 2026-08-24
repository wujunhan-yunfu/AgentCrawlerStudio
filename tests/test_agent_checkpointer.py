"""backend.services.agent.checkpointer 测试。"""

from __future__ import annotations

import pytest
from mongomock import MongoClient as SyncMongoMock
from mongomock_motor import AsyncMongoMockClient


@pytest.fixture()
def reset_factory():
    from backend.services.agent.checkpointer import CheckpointerFactory

    CheckpointerFactory._saver = None
    CheckpointerFactory._client = None
    CheckpointerFactory._async_client = None
    yield
    CheckpointerFactory._saver = None
    CheckpointerFactory._client = None
    CheckpointerFactory._async_client = None


@pytest.fixture()
def mock_mongo_factory(monkeypatch, reset_factory):
    """把 CheckpointerFactory 的客户端替换为 mongomock, 避免真实连接耗时。"""
    from backend.services.agent import checkpointer as cp

    monkeypatch.setattr(cp, "MongoClient", SyncMongoMock)
    monkeypatch.setattr(cp, "AsyncIOMotorClient", AsyncMongoMockClient)


@pytest.fixture()
def clients():
    sync_client = SyncMongoMock()
    async_client = AsyncMongoMockClient()
    yield sync_client, async_client
    sync_client.close()
    async_client.close()


@pytest.fixture()
def saver(clients):
    from backend.services.agent.checkpointer import AsyncMongoDBSaver

    sync_client, async_client = clients
    s = AsyncMongoDBSaver(client=sync_client, async_client=async_client,
                          db_name="crawler")
    return s


def _checkpoint(cid="cp-1"):
    return {
        "id": cid,
        "v": 1,
        "ts": 1.0,
        "channel_values": {"messages": []},
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }


def _config(thread_id="t1", checkpoint_id=None):
    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if checkpoint_id:
        cfg["configurable"]["checkpoint_id"] = checkpoint_id
    return cfg


# --------------------------------------------------------------------------- 索引


async def test_create_saver_indexes_async(clients):
    from backend.services.agent.checkpointer import _create_saver_indexes_async

    sync_client, async_client = clients
    col = async_client["crawler"]["cp"]
    await _create_saver_indexes_async(col, [("thread_id", 1), ("checkpoint_id", -1)], ttl=60)
    idxs = [list(ix["key"].items()) for ix in await col.list_indexes().to_list(None)]
    assert [("thread_id", 1), ("checkpoint_id", -1)] in idxs
    assert [("created_at", 1)] in idxs

    # 幂等: 再创建不报错
    await _create_saver_indexes_async(col, [("thread_id", 1), ("checkpoint_id", -1)], ttl=60)
    # 无 ttl
    await _create_saver_indexes_async(col, [("a", 1)])


async def test_setup_indexes(saver):
    await saver.setup_indexes()
    await saver.setup_indexes()  # 幂等
    assert saver._setup_done is True


def test_close_and_aclose(saver):
    saver.close()
    # aclose 需要异步
    import asyncio

    async def main():
        await saver.aclose()

    asyncio.run(main())


# --------------------------------------------------------------------------- 同步方法


def test_put_get_tuple(saver):
    cfg = _config("t1")
    ret = saver.put(cfg, _checkpoint("cp-1"), {"source": "test"}, {})
    assert ret["configurable"]["checkpoint_id"] == "cp-1"
    ret2 = saver.put(cfg, _checkpoint("cp-2"), {"source": "test"}, {})
    # get 最新
    got = saver.get_tuple(_config("t1"))
    assert got.checkpoint["id"] == "cp-2"
    # 指定 id
    got2 = saver.get_tuple(_config("t1", "cp-1"))
    assert got2.checkpoint["id"] == "cp-1"
    assert got2.metadata == {"source": "test"}
    assert got2.parent_config is None


def test_get_tuple_parent_and_writes(saver):
    cfg = _config("t1")
    saver.put(cfg, _checkpoint("cp-1"), {}, {})
    saver.put(_config("t1", "cp-1"), _checkpoint("cp-2"), {}, {})
    got = saver.get_tuple(_config("t1", "cp-2"))
    assert got.parent_config["configurable"]["checkpoint_id"] == "cp-1"
    saver.put_writes(_config("t1", "cp-2"), [("messages", {"x": 1})], task_id="task1")
    got2 = saver.get_tuple(_config("t1", "cp-2"))
    assert len(got2.pending_writes) == 1
    assert got2.pending_writes[0][0] == "task1"


def test_get_tuple_missing(saver):
    assert saver.get_tuple(_config("nope")) is None


def test_list(saver):
    saver.put(_config("t1"), _checkpoint("cp-1"), {}, {})
    saver.put(_config("t1"), _checkpoint("cp-2"), {"tag": "x"}, {})
    saver.put(_config("t2"), _checkpoint("cp-3"), {}, {})
    items = list(saver.list(_config("t1")))
    assert len(items) == 2
    items2 = list(saver.list(_config("t1"), limit=1))
    assert len(items2) == 1
    items3 = list(saver.list(_config("t1"), filter={"tag": "x"}))
    assert len(items3) == 1
    items4 = list(saver.list(_config("t1"), before=_config("t1", "cp-2")))
    assert len(items4) == 1
    # config 为空
    items5 = list(saver.list(None))
    assert len(items5) == 3


def test_put_ttl(saver):
    saver.ttl = 60
    saver.put(_config("t1"), _checkpoint("cp-1"), {}, {})
    got = saver.get_tuple(_config("t1"))
    assert got is not None


def test_put_writes_set_on_insert(saver):
    cfg = _config("t1", "cp-1")
    saver.put_writes(cfg, [("not-mapped", 1)], task_id="t")
    got = saver.get_tuple(_config("t1", "cp-1"))
    # writes 存在(未通过 get_tuple 的 pending_writes 读取——需直接查)
    from backend.services.agent.checkpointer import WRITES_COLLECTION

    wcol = saver.async_db[WRITES_COLLECTION]
    # 用 sync
    docs = list(saver.writes_collection.find({}))
    assert docs


def test_delete_thread(saver):
    saver.put(_config("t1"), _checkpoint("cp-1"), {}, {})
    saver.put_writes(_config("t1", "cp-1"), [("messages", "x")], task_id="t")
    saver.delete_thread("t1")
    assert saver.get_tuple(_config("t1")) is None
    assert len(list(saver.writes_collection.find({}))) == 0


# --------------------------------------------------------------------------- 异步方法


async def test_aput_aget_tuple(saver):
    ret = await saver.aput(_config("t1"), _checkpoint("cp-1"), {"m": 1}, {})
    assert ret["configurable"]["checkpoint_id"] == "cp-1"
    got = await saver.aget_tuple(_config("t1"))
    assert got.checkpoint["id"] == "cp-1"
    assert got.metadata == {"m": 1}
    got2 = await saver.aget_tuple(_config("t1", "cp-1"))
    assert got2.checkpoint["id"] == "cp-1"
    assert await saver.aget_tuple(_config("nope")) is None


async def test_aput_parent(saver):
    await saver.aput(_config("t1"), _checkpoint("cp-1"), {}, {})
    await saver.aput(_config("t1", "cp-1"), _checkpoint("cp-2"), {}, {})
    got = await saver.aget_tuple(_config("t1", "cp-2"))
    assert got.parent_config["configurable"]["checkpoint_id"] == "cp-1"


async def test_alist(saver):
    await saver.aput(_config("t1"), _checkpoint("cp-1"), {}, {})
    await saver.aput(_config("t1"), _checkpoint("cp-2"), {"tag": "y"}, {})
    items = [cp async for cp in saver.alist(_config("t1"))]
    assert len(items) == 2
    items2 = [cp async for cp in saver.alist(_config("t1"), limit=1)]
    assert len(items2) == 1
    items3 = [cp async for cp in saver.alist(_config("t1"), filter={"tag": "y"})]
    assert len(items3) == 1
    items4 = [cp async for cp in saver.alist(None)]
    assert len(items4) == 2
    items5 = [cp async for cp in saver.alist(_config("t1"), before=_config("t1", "cp-2"))]
    assert len(items5) == 1


async def test_aput_writes_and_adelete(saver):
    await saver.aput(_config("t1"), _checkpoint("cp-1"), {}, {})
    await saver.aput_writes(_config("t1", "cp-1"), [("messages", "v")], task_id="t")
    got = await saver.aget_tuple(_config("t1", "cp-1"))
    assert len(got.pending_writes) == 1
    await saver.adelete_thread("t1")
    assert await saver.aget_tuple(_config("t1")) is None


# --------------------------------------------------------------------------- 工厂


def test_get_saver_requires_uri(reset_factory):
    from backend.services.agent.checkpointer import CheckpointerFactory

    with pytest.raises(ValueError, match="MongoDB URL"):
        CheckpointerFactory.get_saver("", "crawler")


def test_get_saver_singleton(mock_mongo_factory):
    from backend.services.agent.checkpointer import CheckpointerFactory

    s1 = CheckpointerFactory.get_saver("mongodb://fake", "crawler")
    s2 = CheckpointerFactory.get_saver("mongodb://fake", "crawler")
    assert s1 is s2


async def test_setup_and_close(mock_mongo_factory):
    from backend.services.agent.checkpointer import CheckpointerFactory

    saver = await CheckpointerFactory.setup("mongodb://fake", "crawler")
    assert saver is not None
    await CheckpointerFactory.close()
    assert CheckpointerFactory._saver is None


async def test_setup_index_failure(reset_factory, monkeypatch):
    from backend.services.agent.checkpointer import CheckpointerFactory

    class BoomSaver:
        async def setup_indexes(self):
            raise RuntimeError("mongo down")

    monkeypatch.setattr(CheckpointerFactory, "get_saver", lambda *a, **kw: BoomSaver())
    saver = await CheckpointerFactory.setup("mongodb://fake", "crawler")
    assert isinstance(saver, BoomSaver)


def test_get_checkpointer(cfg, mock_mongo_factory):
    from backend.services.agent.checkpointer import get_checkpointer

    cfg.mongo_uri = "mongodb://fake"
    s = get_checkpointer(cfg)
    assert s is not None


async def test_setup_close_checkpointer(cfg, mock_mongo_factory):
    from backend.services.agent.checkpointer import close_checkpointer, setup_checkpointer

    cfg.mongo_uri = "mongodb://fake"
    await setup_checkpointer(cfg)
    await close_checkpointer()
