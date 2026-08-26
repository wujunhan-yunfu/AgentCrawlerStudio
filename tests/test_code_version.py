"""backend.services.code_version + backend.routers.versions 测试。

- CodeStore: 提交 / 列表 / 查详情 / 判重 / HEAD 更新 / 隔离 / MongoDB 故障冷却降级;
- 路由(make_test_app + httpx.ASGITransport): repo / commit(含 400/422) / commits /
  commits/{id} / checkout(含 404); Mongo 不可达时读降级 / 写 503。
"""

from __future__ import annotations

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from conftest import make_test_app
from backend.services.code_version import (
    CodeStore,
    NoChangeError,
    commit_hash,
    content_hash,
    diff_stat,
)


@pytest.fixture()
def mongo_client():
    client = AsyncMongoMockClient()
    yield client
    client.close()


@pytest.fixture()
def store(mongo_client, monkeypatch):
    monkeypatch.setattr("backend.services.code_version.AsyncIOMotorClient",
                        lambda uri, **kw: mongo_client)
    return CodeStore("mongodb://fake", "crawler")


@pytest.fixture()
def mongo_down_store(mongo_client, monkeypatch):
    monkeypatch.setattr("backend.services.code_version.AsyncIOMotorClient",
                        lambda uri, **kw: (_ for _ in ()).throw(RuntimeError("no mongo")))
    return CodeStore("mongodb://fake", "crawler")


# --------------------------------------------------------------- 哈希与统计工具


def test_content_hash_deterministic():
    h1 = content_hash("print(1)")
    h2 = content_hash("print(1)")
    assert h1 == h2
    assert len(h1) == 40
    assert content_hash("print(1)") != content_hash("print(2)")


def test_commit_hash_deterministic_and_unique():
    a = commit_hash("cid", "parent1", "code", "msg", 1000)
    b = commit_hash("cid", "parent1", "code", "msg", 1000)
    assert a == b
    assert a != commit_hash("cid", "parent1", "code", "msg", 1001)
    assert a != commit_hash("cid", "parent2", "code", "msg", 1000)
    assert a != commit_hash("cid", "parent1", "code2", "msg", 1000)
    assert commit_hash("cid", None, "code", "msg", 1000) == commit_hash(
        "cid", None, "code", "msg", 1000
    )


def test_diff_stat():
    assert diff_stat("", "a\nb\n") == {"add": 2, "del": 0}
    assert diff_stat("a\nb\nc\n", "a\nb\nc\n") == {"add": 0, "del": 0}
    st = diff_stat("a\nb\nc\n", "a\nx\nc\nd\n")
    assert st["add"] == 2  # x, d
    assert st["del"] == 1  # b
    assert diff_stat("a\n", "") == {"add": 0, "del": 1}


# --------------------------------------------------------------- CodeStore


async def test_store_first_commit(store):
    commit = await store.create_commit("cid", "首提交", "print(1)", "dev")
    assert commit["parent"] is None
    assert commit["message"] == "首提交"
    assert commit["content"] == "print(1)"
    assert commit["author"] == "dev"
    assert commit["content_hash"] == content_hash("print(1)")
    assert commit["size"] == len("print(1)")
    assert await store.get_head("cid") == commit["commit_id"]


async def test_store_commit_chain(store):
    c1 = await store.create_commit("cid", "v1", "a=1", "dev")
    c2 = await store.create_commit("cid", "v2", "a=1\nb=2", "dev")
    assert c2["parent"] == c1["commit_id"]
    assert await store.get_head("cid") == c2["commit_id"]


async def test_store_no_change_raises(store):
    await store.create_commit("cid", "v1", "code", "dev")
    with pytest.raises(NoChangeError):
        await store.create_commit("cid", "v2", "code", "dev")


async def test_store_commit_isolation(store):
    await store.create_commit("cidA", "a", "A", "dev")
    cidB = await store.create_commit("cidB", "b", "B", "dev")
    assert cidB["parent"] is None
    # A 的 HEAD 不受 B 影响
    head_a = await store.get_head("cidA")
    commit_a = await store.get_commit("cidA", head_a)
    assert commit_a["content"] == "A"


async def test_store_get_commit_missing(store):
    assert await store.get_commit("cid", "nope") is None


async def test_store_list_commits_order_and_stat(store):
    await store.create_commit("cid", "v1", "a\nb\n", "dev")
    await store.create_commit("cid", "v2", "a\nb\nc\n", "dev")
    await store.create_commit("cid", "v3", "a\nb\nc\nd\n", "dev")
    commits = await store.list_commits("cid")
    assert len(commits) == 3
    # 倒序: 最新在前
    assert commits[0]["message"] == "v3"
    assert commits[2]["message"] == "v1"
    # 列表不含全量 content
    assert "content" not in commits[0]
    # 首提交 stat 为全部行
    assert commits[2]["stat"] == {"add": 2, "del": 0}
    # 第二次提交相对父: +1 行
    assert commits[1]["stat"] == {"add": 1, "del": 0}
    assert commits[0]["stat"] == {"add": 1, "del": 0}


async def test_store_list_commits_before_pagination(store):
    await store.create_commit("cid", "v1", "a", "dev")
    c2 = await store.create_commit("cid", "v2", "b", "dev")
    await store.create_commit("cid", "v3", "c", "dev")
    page = await store.list_commits("cid", before=c2["commit_id"])
    assert len(page) == 1
    assert page[0]["message"] == "v1"


async def test_store_connect_failure_and_cooldown(store, mongo_client, monkeypatch):
    monkeypatch.setattr("backend.services.code_version.AsyncIOMotorClient",
                        lambda uri, **kw: (_ for _ in ()).throw(RuntimeError("no mongo")))
    with pytest.raises(ConnectionError, match="MongoDB 不可达"):
        await store.create_commit("cid", "v", "x", "dev")
    assert store._down_until > 0
    with pytest.raises(ConnectionError, match="冷却"):
        await store._connect()


async def test_store_empty_mongo_uri_raises(monkeypatch):
    store = CodeStore("", "crawler")
    with pytest.raises(ConnectionError, match="未配置"):
        await store._connect()


# --------------------------------------------------------------- 路由


@pytest.fixture()
def app_with_store(mongo_client, monkeypatch):
    monkeypatch.setattr("backend.services.code_version.AsyncIOMotorClient",
                        lambda uri, **kw: mongo_client)
    store = CodeStore("mongodb://fake", "crawler")
    app = make_test_app()
    app.state.code_store = store
    return app


@pytest.fixture()
async def client(app_with_store):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_store), base_url="http://test"
    ) as c:
        yield c


async def test_repo_empty(client):
    resp = await client.get("/api/v1/code/repo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_commits"] is False
    assert body["head"] is None
    assert body["crawler_id"] == "dev_test"


async def test_commit_then_repo_and_list(client):
    r = await client.post("/api/v1/code/commit", json={
        "message": "修复翻页逻辑", "content": "a=1", "author": "dev",
    })
    assert r.status_code == 200
    commit = r.json()["commit"]
    assert commit["content"] == "a=1"
    assert commit["parent"] is None

    repo = (await client.get("/api/v1/code/repo")).json()
    assert repo["has_commits"] is True
    assert repo["head"]["commit_id"] == commit["commit_id"]

    lst = (await client.get("/api/v1/code/commits")).json()
    assert len(lst["commits"]) == 1
    assert lst["commits"][0]["commit_id"] == commit["commit_id"]
    assert lst["commits"][0]["stat"] == {"add": 1, "del": 0}


async def test_commit_chain_stat(client):
    await client.post("/api/v1/code/commit", json={"message": "v1", "content": "a\nb\n"})
    r2 = await client.post("/api/v1/code/commit", json={"message": "v2", "content": "a\nb\nc\n"})
    lst = (await client.get("/api/v1/code/commits")).json()
    assert len(lst["commits"]) == 2
    assert lst["commits"][0]["message"] == "v2"
    assert lst["commits"][0]["stat"] == {"add": 1, "del": 0}
    assert lst["commits"][1]["stat"] == {"add": 2, "del": 0}


async def test_commit_no_change_400(client):
    await client.post("/api/v1/code/commit", json={"message": "v1", "content": "same"})
    r = await client.post("/api/v1/code/commit", json={"message": "v2", "content": "same"})
    assert r.status_code == 400
    assert "无变更" in r.json()["detail"]


async def test_commit_empty_message_422(client):
    r = await client.post("/api/v1/code/commit", json={"message": "   ", "content": "x"})
    assert r.status_code == 422
    r2 = await client.post("/api/v1/code/commit", json={"message": "", "content": "x"})
    assert r2.status_code == 422
    r3 = await client.post("/api/v1/code/commit", json={"content": "x"})
    assert r3.status_code == 422


async def test_commit_missing_content_422(client):
    r = await client.post("/api/v1/code/commit", json={"message": "m"})
    assert r.status_code == 422


async def test_commit_detail(client):
    r = await client.post("/api/v1/code/commit", json={"message": "v1", "content": "code"})
    cid = r.json()["commit"]["commit_id"]
    resp = await client.get(f"/api/v1/code/commits/{cid}")
    assert resp.status_code == 200
    assert resp.json()["content"] == "code"
    assert resp.json()["commit_id"] == cid


async def test_commit_detail_missing_404(client):
    resp = await client.get("/api/v1/code/commits/does-not-exist")
    assert resp.status_code == 404


async def test_checkout(client):
    r = await client.post("/api/v1/code/commit", json={"message": "v1", "content": "hello"})
    cid = r.json()["commit"]["commit_id"]
    resp = await client.post("/api/v1/code/checkout", json={"commit_id": cid})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["code"] == "hello"
    assert body["commit_id"] == cid


async def test_checkout_missing_404(client):
    resp = await client.post("/api/v1/code/checkout", json={"commit_id": "nope"})
    assert resp.status_code == 404


async def test_crawler_id_isolation_via_api(app_with_store):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_store), base_url="http://test"
    ) as c:
        await c.post("/api/v1/code/commit",
                     json={"message": "a", "content": "A", "crawler_id": "devA"})
        other = (await c.get("/api/v1/code/repo?crawler_id=devB")).json()
        assert other["has_commits"] is False
        mine = (await c.get("/api/v1/code/repo?crawler_id=devA")).json()
        assert mine["has_commits"] is True


async def test_mongo_down_read_degrades_and_write_503(mongo_down_store):
    app = make_test_app()
    app.state.code_store = mongo_down_store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        repo = (await c.get("/api/v1/code/repo")).json()
        assert repo["has_commits"] is False
        assert repo["head"] is None
        lst = (await c.get("/api/v1/code/commits")).json()
        assert lst["commits"] == []
        r = await c.post("/api/v1/code/commit",
                         json={"message": "m", "content": "x"})
        assert r.status_code == 503
        r2 = await c.post("/api/v1/code/checkout", json={"commit_id": "x"})
        assert r2.status_code == 503


async def test_default_crawler_id_fallback(client):
    repo = (await client.get("/api/v1/code/repo")).json()
    assert repo["crawler_id"] == "dev_test"  # Config() 默认 crawler_id


async def test_lazy_store_degrades_when_mongo_unconfigured():
    """未注入 code_store 时路由惰性创建; Config() 默认 mongo_uri 为空 → 读接口降级。"""
    app = make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        repo = (await c.get("/api/v1/code/repo")).json()
        assert repo["has_commits"] is False
        assert repo["head"] is None
        lst = (await c.get("/api/v1/code/commits")).json()
        assert lst["commits"] == []
        # 惰性创建的 store 被挂在 app.state 上
        assert app.state.code_store is not None


async def test_commits_bad_limit(client):
    resp = await client.get("/api/v1/code/commits?limit=abc")
    assert resp.status_code == 200
    assert resp.json()["commits"] == []


async def test_commit_detail_mongo_down_503(mongo_down_store):
    app = make_test_app()
    app.state.code_store = mongo_down_store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/api/v1/code/commits/any")
        assert resp.status_code == 503