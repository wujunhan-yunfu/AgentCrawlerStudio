"""backend.services.network / dom / storage 频道测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import FakeCdpMgr


# --------------------------------------------------------------------------- Network


@pytest.fixture()
def network(fake_cdp_mgr):
    from backend.services.network import NetworkChannel

    ch = NetworkChannel(fake_cdp_mgr)
    return ch


async def test_network_on_event_unknown(network, fake_cdp_mgr):
    assert await network.on_event(fake_cdp_mgr.session, "Foo.x", {}) is False


async def test_network_request_flow(network, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    await network.on_event(session, "Network.requestWillBeSent", {
        "requestId": "r1",
        "request": {"url": "http://a/x", "method": "GET", "headers": {"a": "1"},
                    "postData": "body"},
        "type": "XHR",
        "wallTime": 100.0,
        "timestamp": 100.0,
        "initiator": {"type": "script", "stack": {
            "callFrames": [{"url": "http://a/app.js", "lineNumber": 1}]}},
    })
    rec = network._records["r1"]
    assert rec["url"] == "http://a/x"
    assert rec["postData"] == "body"
    assert rec["initiator"] == "http://a/app.js:2"

    await network.on_event(session, "Network.responseReceived", {
        "requestId": "r1",
        "response": {"status": 200, "statusText": "OK", "mimeType": "text/html",
                     "headers": {"ct": "text/html"}},
    })
    assert network._records["r1"]["status"] == 200

    await network.on_event(session, "Network.loadingFinished", {
        "requestId": "r1", "timestamp": 100.5, "encodedDataLength": 1024,
    })
    assert network._records["r1"]["duration"] == 500.0
    assert network._records["r1"]["size"] == 1024

    sub = await network.channel.attach()
    await asyncio.sleep(0)
    got = []
    while True:
        try:
            item = await asyncio.wait_for(sub.wait(timeout=0.05), 0.05)
        except asyncio.TimeoutError:
            break
        if item:
            got.append(json.loads(item))
    await network.channel.detach(sub)
    ops = [g["op"] for g in got]
    assert "request" in ops and "response" in ops and "finished" in ops


async def test_network_failed(network, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    await network.on_event(session, "Network.requestWillBeSent", {
        "requestId": "r2", "request": {"url": "http://a", "method": "GET"},
        "wallTime": 1.0})
    await network.on_event(session, "Network.loadingFailed", {
        "requestId": "r2", "errorText": "net::ERR_FAILED", "canceled": True,
        "timestamp": 2.0})
    rec = network._records["r2"]
    assert rec["error"] == "net::ERR_FAILED"
    assert rec["canceled"] is True


async def test_network_frame_navigated(network, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    assert await network.on_event(session, "Page.frameNavigated", {
        "frame": {"url": "http://a"}}) is True
    # 子 frame 不发布
    assert await network.on_event(session, "Page.frameNavigated", {
        "frame": {"url": "http://a", "parentId": "p"}}) is False


async def test_network_orphan_events(network, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    # response/loading 找不到记录 -> 直接返回 True 不报错
    assert await network.on_event(session, "Network.responseReceived", {
        "requestId": "ghost"}) is True
    assert await network.on_event(session, "Network.loadingFinished", {
        "requestId": "ghost"}) is True
    assert await network.on_event(session, "Network.loadingFailed", {
        "requestId": "ghost"}) is True


def test_initiator_text():
    from backend.services.network import NetworkChannel

    assert NetworkChannel._initiator_text(None) is None
    assert NetworkChannel._initiator_text({}) is None
    assert NetworkChannel._initiator_text({"type": "other"}) == "other"
    assert NetworkChannel._initiator_text({"type": "script", "stack": {}}) == "script"
    assert NetworkChannel._initiator_text({"type": "script", "stack": {
        "callFrames": [{"url": "http://a/x.js", "lineNumber": 2}]}}) == "http://a/x.js:3"
    assert NetworkChannel._initiator_text({"type": "script", "stack": {
        "callFrames": [{"url": ""}]}}) == "<inline>:1"


def test_post_data():
    from backend.services.network import NetworkChannel

    assert NetworkChannel._post_data({"postData": "x"}) == "x"
    assert NetworkChannel._post_data({}) is None


async def test_network_clear(network, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    await network.on_event(session, "Network.requestWillBeSent", {
        "requestId": "r1", "request": {"url": "u", "method": "GET"}})
    res = await network.clear()
    assert res == {"ok": True}
    assert network._records == {}
    assert network._start_ts == {}
    assert len(network.channel._history) == 1  # clear 事件


async def test_network_duration_correct(network, fake_cdp_mgr):
    """耗时基于 CDP 单调时间戳计算, 而不是 wallTime 与 timestamp 混算(旧实现恒为 0)。"""
    session = fake_cdp_mgr.session
    await network.on_event(session, "Network.requestWillBeSent", {
        "requestId": "r9",
        "wallTime": 1700000000.0,  # epoch, 供前端展示
        "timestamp": 100.0,        # 单调时间戳, 用于耗时
        "request": {"url": "https://a.com", "method": "GET"},
    })
    await network.on_event(session, "Network.loadingFinished", {
        "requestId": "r9", "timestamp": 112.5, "encodedDataLength": 2048})
    record = network._records["r9"]
    assert record["started"] == 1700000000.0
    assert record["duration"] == pytest.approx(12500.0)  # 12.5s × 1000


async def test_network_failed_duration_correct(network, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    await network.on_event(session, "Network.requestWillBeSent", {
        "requestId": "r10", "timestamp": 5.0,
        "request": {"url": "https://a.com", "method": "GET"},
    })
    await network.on_event(session, "Network.loadingFailed", {
        "requestId": "r10", "timestamp": 6.0, "errorText": "ERR", "canceled": True})
    record = network._records["r10"]
    assert record["duration"] == 1000.0
    assert record["error"] == "ERR"
    assert record["canceled"] is True


async def test_network_body(network, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    session.responses = {
        "Network.getResponseBody": {"id": 1, "result": {
            "body": "hello", "base64Encoded": False}},
    }
    network._session_of["r1"] = "ws://fake"
    res = await network.body("r1")
    assert res["ok"] is True
    assert res["body"] == "hello"

    # CDP error
    session.responses["Network.getResponseBody"] = {"id": 1, "error": {"message": "no body"}}
    res2 = await network.body("r1")
    assert res2["ok"] is False

    # 无会话
    fake_cdp_mgr.primary_session = None
    res3 = await network.body("r1")
    assert res3["ok"] is False
    fake_cdp_mgr.primary_session = session

    # 命令抛异常
    class Boom:
        def __init__(self):
            self.ws_url = "ws://other"

        async def command(self, m, p=None, timeout=5.0):
            raise RuntimeError("closed")

    network._session_of["r2"] = "ws://other"
    fake_cdp_mgr.primary_session = Boom()
    res4 = await network.body("r2")
    assert res4["ok"] is False

    # 记录的会话返回 error → 回退到活动会话再试一次
    from backend.services.network import NetworkChannel

    class _ErrSession:
        ws_url = "ws://stale"

        async def command(self, m, p=None, timeout=5.0):
            return {"id": 1, "error": {"message": "session gone"}}

    class _OkPrimary:
        ws_url = "ws://active"

        async def command(self, m, p=None, timeout=5.0):
            return {"id": 1, "result": {"body": "fresh", "base64Encoded": False}}

    class _StaleMgr(FakeCdpMgr):
        def __init__(self, stale, primary_ok):
            super().__init__()
            self._stale = stale
            self.primary_session = primary_ok

        def session_for(self, ws_url):
            if ws_url == "ws://stale":
                return self._stale
            return self.primary_session

    mgr = _StaleMgr(_ErrSession(), _OkPrimary())
    net2 = NetworkChannel(mgr)
    net2._session_of["r3"] = "ws://stale"
    res5 = await net2.body("r3")
    assert res5["ok"] is True
    assert res5["body"] == "fresh"


# --------------------------------------------------------------------------- DOM


@pytest.fixture()
def dom(fake_cdp_mgr):
    from backend.services.dom import DOMChannel

    return DOMChannel(fake_cdp_mgr)


async def test_dom_on_event(dom, fake_cdp_mgr):
    assert await dom.on_event(fake_cdp_mgr.session, "DOM.documentUpdated", {}) is True
    assert await dom.on_event(fake_cdp_mgr.session, "Other.x", {}) is False


async def test_dom_tree(dom, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    session.responses = {
        "DOM.getDocument": {"id": 1, "result": {"root": {
            "backendNodeId": 1, "nodeType": 9, "nodeName": "#document",
            "childNodeCount": 1,
            "attributes": ["class", "x", "id", "y"],
            "children": [{
                "backendNodeId": 2, "nodeType": 1, "nodeName": "DIV",
                "attributes": ["a", "b"],
                "contentDocument": {
                    "backendNodeId": 3, "nodeType": 1, "nodeName": "IFRAME",
                },
            }],
        }}},
    }
    res = await dom.tree()
    assert res["ok"] is True
    root = res["root"]
    assert root["attrs"] == {"class": "x", "id": "y"}
    assert root["children"][0]["attrs"] == {"a": "b"}
    assert root["children"][0]["children"][0]["id"] == 3

    # CDP error
    session.responses["DOM.getDocument"] = {"id": 1, "error": {"message": "fail"}}
    res2 = await dom.tree()
    assert res2["ok"] is False

    # 无会话
    fake_cdp_mgr.primary_session = None
    res3 = await dom.tree()
    assert res3["ok"] is False

    # 命令抛异常
    class Boom:
        async def command(self, m, p=None, timeout=5.0):
            raise RuntimeError("boom")

    fake_cdp_mgr.primary_session = Boom()
    res4 = await dom.tree()
    assert res4["ok"] is False


async def test_dom_box_model(dom, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    session.responses = {"DOM.getBoxModel": {"id": 1, "result": {"model": {
        "content": [0, 0, 100, 0, 100, 50, 0, 50]}}}}
    res = await dom.box_model(5)
    assert res["ok"] is True
    assert res["box"] == {"x": 0, "y": 0, "w": 100, "h": 50}

    # 无 content
    session.responses["DOM.getBoxModel"] = {"id": 1, "result": {"model": {}}}
    res2 = await dom.box_model(5)
    assert res2["ok"] is True
    assert res2["box"] is None

    # error
    session.responses["DOM.getBoxModel"] = {"id": 1, "error": {"message": "e"}}
    res3 = await dom.box_model(5)
    assert res3["ok"] is False

    fake_cdp_mgr.primary_session = None
    res4 = await dom.box_model(5)
    assert res4["ok"] is False


def test_dom_transform():
    from backend.services.dom import DOMChannel

    out = DOMChannel._transform({
        "backendNodeId": 1, "nodeType": 3, "nodeName": "#text", "nodeValue": "hi",
    })
    assert out["id"] == 1
    assert out["value"] == "hi"


# --------------------------------------------------------------------------- Storage


@pytest.fixture()
def storage(fake_cdp_mgr):
    from backend.services.storage import StorageChannel

    return StorageChannel(fake_cdp_mgr)


async def test_storage_on_event(storage, fake_cdp_mgr):
    assert await storage.on_event(fake_cdp_mgr.session, "DOMStorage.domStorageItemAdded", {
        "storageId": {"storageKey": "http://a/", "isLocalStorage": True}}) is True
    assert await storage.on_event(fake_cdp_mgr.session, "DOMStorage.domStorageItemRemoved", {
        "storageId": {"storageKey": "http://a/", "isLocalStorage": True}}) is True
    assert await storage.on_event(fake_cdp_mgr.session, "DOMStorage.domStorageItemUpdated", {
        "storageId": {"storageKey": "http://a/", "isLocalStorage": True}}) is True
    assert await storage.on_event(fake_cdp_mgr.session, "DOMStorage.domStorageItemsCleared", {
        "storageId": {"storageKey": "http://a/", "isLocalStorage": False}}) is True
    assert await storage.on_event(fake_cdp_mgr.session, "Other.x", {}) is False


class _BoomSession:
    async def command(self, method, params=None, timeout=5.0):
        raise RuntimeError("cdp down")


async def test_storage_command_exceptions(storage, fake_cdp_mgr):
    fake_cdp_mgr.primary_session = _BoomSession()
    res = await storage.items("http://a")
    assert res["ok"] is False and "cdp down" in res["error"]
    res = await storage.set_item("http://a", False, "k", "v")
    assert res["ok"] is False
    res = await storage.remove_item("http://a", False, "k")
    assert res["ok"] is False
    res = await storage.cookies("http://a")
    assert res["ok"] is False
    res = await storage.set_cookie("http://a", "n", "v")
    assert res["ok"] is False
    res = await storage.delete_cookie("http://a", "n")
    assert res["ok"] is False
    res = await storage.idb_databases("http://a")
    assert res["ok"] is False
    res = await storage.idb_stores("http://a", "db")
    assert res["ok"] is False
    res = await storage.idb_data("http://a", "db", "store")
    assert res["ok"] is False


async def test_storage_origin(storage, fake_cdp_mgr):
    fake_cdp_mgr.session.responses = {
        "Runtime.evaluate": {"id": 1, "result": {
            "result": {"type": "string", "value": "http://example.com"}}},
    }
    res = await storage.origin()
    assert res == {"ok": True, "origin": "http://example.com"}

    # 无值 -> null
    fake_cdp_mgr.session.responses["Runtime.evaluate"] = {"id": 1, "result": {
        "result": {"type": "string", "value": "undefined"}}}
    res2 = await storage.origin()
    assert res2["origin"] == "null"

    # evaluate 失败
    from conftest import FakeCdpMgr as _FakeCdpMgr

    class FailMgr(_FakeCdpMgr):
        async def evaluate(self, expression, timeout=5.0):
            return {"ok": False, "error": "no browser"}

    storage.mgr = FailMgr()
    res3 = await storage.origin()
    assert res3 == {"ok": False, "error": "no browser"}
    storage.mgr = fake_cdp_mgr


def test_storage_id(storage):
    sid = storage._storage_id("http://a", False)
    assert sid == {"storageKey": "http://a/", "isLocalStorage": True}
    sid2 = storage._storage_id("http://a/", True)
    assert sid2["isLocalStorage"] is False


async def test_storage_items(storage, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    session.responses = {"DOMStorage.getDOMStorageItems": {"id": 1, "result": {
        "entries": [["k", "v"], ["k2", "v2"]]}}}
    res = await storage.items("http://a", False)
    assert res["ok"] is True
    assert res["items"] == [{"key": "k", "value": "v"}, {"key": "k2", "value": "v2"}]

    session.responses["DOMStorage.getDOMStorageItems"] = {"id": 1, "error": {"message": "e"}}
    res2 = await storage.items("http://a")
    assert res2["ok"] is False

    fake_cdp_mgr.primary_session = None
    res3 = await storage.items("http://a")
    assert res3["ok"] is False


async def test_storage_set_remove(storage, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    res = await storage.set_item("http://a", False, "k", "v")
    assert res == {"ok": True}
    assert session.command_calls[0][0] == "DOMStorage.setDOMStorageItem"
    res2 = await storage.remove_item("http://a", False, "k")
    assert res2 == {"ok": True}

    session.responses = {"DOMStorage.setDOMStorageItem": {"id": 1, "error": {"message": "dup"}}}
    res3 = await storage.set_item("http://a", False, "k", "v")
    assert res3["ok"] is False

    fake_cdp_mgr.primary_session = None
    res4 = await storage.set_item("http://a", False, "k", "v")
    assert res4["ok"] is False


async def test_storage_cookies(storage, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    session.responses = {"Network.getCookies": {"id": 1, "result": {"cookies": [{"name": "n"}]}}}
    res = await storage.cookies("http://a")
    assert res["ok"] is True
    assert res["cookies"] == [{"name": "n"}]

    session.responses["Network.getCookies"] = {"id": 1, "error": {"message": "e"}}
    res2 = await storage.cookies("http://a")
    assert res2["ok"] is False

    fake_cdp_mgr.primary_session = None
    res3 = await storage.cookies("http://a")
    assert res3["ok"] is False


async def test_storage_set_cookie(storage, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    res = await storage.set_cookie("http://a", "name", "val", domain="example.com", http_only=True)
    assert res == {"ok": True}
    assert session.command_calls[0][0] == "Network.setCookie"
    assert session.command_calls[0][1]["domain"] == "example.com"
    assert session.command_calls[0][1]["httpOnly"] is True

    session.responses = {"Network.setCookie": {"id": 1, "error": {"message": "e"}}}
    res2 = await storage.set_cookie("http://a", "n", "v")
    assert res2["ok"] is False

    fake_cdp_mgr.primary_session = None
    res3 = await storage.set_cookie("http://a", "n", "v")
    assert res3["ok"] is False


async def test_storage_delete_cookie(storage, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    res = await storage.delete_cookie("http://a", "n")
    assert res == {"ok": True}
    session.responses = {"Network.deleteCookies": {"id": 1, "error": {"message": "e"}}}
    res2 = await storage.delete_cookie("http://a", "n")
    assert res2["ok"] is False
    fake_cdp_mgr.primary_session = None
    res3 = await storage.delete_cookie("http://a", "n")
    assert res3["ok"] is False


async def test_storage_idb_databases(storage, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    session.responses = {"IndexedDB.requestDatabaseNames": {"id": 1, "result": {
        "databaseNames": ["db1"]}}}
    res = await storage.idb_databases("http://a")
    assert res == {"ok": True, "databases": ["db1"]}

    session.responses["IndexedDB.requestDatabaseNames"] = {"id": 1, "error": {"message": "e"}}
    res2 = await storage.idb_databases("http://a")
    assert res2["ok"] is False

    fake_cdp_mgr.primary_session = None
    res3 = await storage.idb_databases("http://a")
    assert res3["ok"] is False


async def test_storage_idb_stores(storage, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    session.responses = {"IndexedDB.requestDatabase": {"id": 1, "result": {
        "databaseWithObjectStores": {"objectStores": [
            {"name": "s1", "keyPath": {"type": "string"}, "indexes": [1, 2]},
            {"name": "s2", "indexes": []},
        ]}}}}
    res = await storage.idb_stores("http://a", "db")
    assert res["ok"] is True
    assert res["stores"] == [
        {"name": "s1", "keyPath": {"type": "string"}, "indexes": 2},
        {"name": "s2", "keyPath": None, "indexes": 0},
    ]

    session.responses["IndexedDB.requestDatabase"] = {"id": 1, "error": {"message": "e"}}
    res2 = await storage.idb_stores("http://a", "db")
    assert res2["ok"] is False

    fake_cdp_mgr.primary_session = None
    res3 = await storage.idb_stores("http://a", "db")
    assert res3["ok"] is False


async def test_storage_idb_data(storage, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    session.responses = {"IndexedDB.requestData": {"id": 1, "result": {
        "objectStoreDataEntries": [
            {"key": {"type": "number", "value": 1},
             "primaryKey": {"type": "string", "value": "pk"},
             "value": {"type": "string", "value": "v"}},
        ]}}}
    res = await storage.idb_data("http://a", "db", "store", skip=0, count=1)
    assert res["ok"] is True
    assert res["rows"][0]["key"] == "1"
    assert res["has_more"] is True

    session.responses["IndexedDB.requestData"] = {"id": 1, "error": {"message": "e"}}
    res2 = await storage.idb_data("http://a", "db", "store")
    assert res2["ok"] is False

    fake_cdp_mgr.primary_session = None
    res3 = await storage.idb_data("http://a", "db", "store")
    assert res3["ok"] is False


async def test_storage_entry_text(storage, fake_cdp_mgr):
    session = fake_cdp_mgr.session
    assert await storage._entry_text(session, None) == ""
    assert await storage._entry_text(session, {"type": "string", "value": "s"}) == "s"
    assert await storage._entry_text(session, {"type": "number", "value": 5}) == "5"
    assert await storage._entry_text(session, {"type": "bigint", "value": "9"}) == "9"
    assert await storage._entry_text(session, {"type": "other", "value": "x"}) == "x"

    # objectId -> callFunctionOn
    session.responses["Runtime.callFunctionOn"] = {"id": 1, "result": {
        "result": {"type": "string", "value": "{\"a\":1}"}}}
    assert await storage._entry_text(session, {"type": "object", "objectId": "o1"}) == '{"a":1}'

    # callFunctionOn 异常 -> description
    class Boom:
        async def command(self, m, p=None, timeout=5.0):
            raise RuntimeError("boom")

    assert await storage._entry_text(Boom(), {"type": "object", "objectId": "o1",
                                              "description": "desc"}) == "desc"
    assert await storage._entry_text(Boom(), {"type": "object", "objectId": "o1"}) == ""

    # callFunctionOn 返回非字符串
    session.responses["Runtime.callFunctionOn"] = {"id": 1, "result": {
        "result": {"type": "number", "value": 5}}}
    assert await storage._entry_text(session, {"type": "object", "objectId": "o1",
                                               "description": "d"}) == "d"
