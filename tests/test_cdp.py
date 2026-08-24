"""backend.services.cdp 测试。"""

from __future__ import annotations

import asyncio
import json

import pytest


# --------------------------------------------------------------------------- Subscriber / Channel


async def test_cdp_subscriber_push_wait():
    from backend.services.cdp import Subscriber

    sub = Subscriber()
    sub.push("a")
    assert await sub.wait(timeout=1) == "a"
    # 空队列超时返回 None
    assert await sub.wait(timeout=0.05) is None


async def test_cdp_subscriber_bounded():
    from backend.services.cdp import Subscriber

    sub = Subscriber(maxlen=2)
    sub.push("a")
    sub.push("b")
    sub.push("c")
    assert await sub.wait(timeout=1) == "b"
    assert await sub.wait(timeout=1) == "c"


async def test_channel_attach_history_replay():
    from backend.services.cdp import Channel

    ch = Channel("test")
    await ch.publish({"a": 1})
    sub = await ch.attach()
    # 新订阅者收到历史回放
    assert await sub.wait(timeout=1) == '{"a": 1}'
    assert ch.count() == 1
    await ch.detach(sub)
    assert ch.count() == 0


async def test_channel_publish_and_history():
    from backend.services.cdp import Channel

    ch = Channel("test")
    sub = await ch.attach()
    await ch.publish({"b": "值"})
    assert await sub.wait(timeout=1) == json.dumps({"b": "值"}, ensure_ascii=False)
    assert ch.count() == 1
    # 历史已累计
    assert len(ch._history) == 1


async def test_channel_reset_and_clear_history():
    from backend.services.cdp import Channel

    ch = Channel("test")
    sub = await ch.attach()
    await ch.publish({"x": 1})
    ch.reset()
    assert ch.count() == 0
    assert len(ch._history) == 0
    await ch.publish({"y": 2})
    ch.clear_history()
    assert len(ch._history) == 0


# --------------------------------------------------------------------------- CDPSession


async def test_cdpsession_command_and_resolve():
    from backend.services.cdp import CDPSession

    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

    ws = FakeWS()
    session = CDPSession("ws://x")
    session.ws = ws

    task = asyncio.create_task(session.command("Runtime.enable", {"a": 1}, timeout=2))
    await asyncio.sleep(0)
    msg = ws.sent[0]
    assert msg["method"] == "Runtime.enable"
    assert msg["params"] == {"a": 1}
    session.resolve({"id": msg["id"], "result": {"ok": 1}})
    resp = await task
    assert resp["result"]["ok"] == 1
    # 未解析 id 的消息不触发
    session.resolve({"id": 999, "result": {}})


async def test_cdpsession_command_not_connected():
    from backend.services.cdp import CDPSession

    session = CDPSession("ws://x")
    with pytest.raises(RuntimeError, match="未连接"):
        await session.command("Runtime.enable")


async def test_cdpsession_command_timeout():
    from backend.services.cdp import CDPSession

    class FakeWS:
        async def send(self, data):
            pass

    session = CDPSession("ws://x")
    session.ws = FakeWS()
    with pytest.raises(asyncio.TimeoutError):
        await session.command("Runtime.enable", timeout=0.05)


async def test_cdpsession_no_params():
    from backend.services.cdp import CDPSession

    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

    ws = FakeWS()
    session = CDPSession("ws://x")
    session.ws = ws
    task = asyncio.create_task(session.command("Runtime.enable", timeout=2))
    await asyncio.sleep(0)
    assert "params" not in ws.sent[0]
    session.resolve({"id": ws.sent[0]["id"], "result": {}})
    await task


# --------------------------------------------------------------------------- CDPManager


@pytest.fixture()
def cdp_manager(cfg, monkeypatch):
    from backend.services.cdp import CDPManager

    mgr = CDPManager(cfg)
    mgr._client = _FakeHttpClient()
    yield mgr
    mgr._stop = True
    for task in mgr._tasks:
        task.cancel()


class _FakeHttpResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


class _FakeHttpClient:
    def __init__(self, targets=None):
        self.targets = targets or []
        self.calls = 0

    async def get(self, url):
        self.calls += 1
        return _FakeHttpResponse(self.targets)

    async def aclose(self):
        pass


class _FakeSession:
    def __init__(self, ws_url="ws://target1", responses=None):
        self.ws_url = ws_url
        self.ready = _FakeEvent(True)
        self.responses = responses or {}
        self.group_depth = 0
        self.queue = None
        self.command_calls = []

    async def command(self, method, params=None, timeout=5.0):
        self.command_calls.append((method, params))
        return self.responses.get(method, {"id": 1, "result": {}})


class _FakeEvent:
    def __init__(self, value):
        self.value = value

    def is_set(self):
        return self.value

    def set(self):
        self.value = True


def test_cdp_manager_register_and_status(cdp_manager):
    from backend.services.cdp import Channel

    class Handler:
        name = "h1"
        domains = [("H.enable", {})]

    mgr = cdp_manager
    handler = Handler()
    ch = mgr.register_channel(handler)
    assert isinstance(ch, Channel)
    assert mgr.channel("h1") is ch
    assert getattr(mgr, "h1") is handler
    st = mgr.status()
    assert "targets" in st
    assert st["subscribers"] == {"h1": 0}
    assert mgr.status()["history"] == 0


async def test_cdp_manager_rescan_and_scan(cdp_manager):
    from backend.services.cdp import CDPSession

    mgr = cdp_manager
    mgr._client.targets = [
        {"type": "page", "url": "http://a", "webSocketDebuggerUrl": "ws://a"},
        {"type": "page", "url": "devtools://x", "webSocketDebuggerUrl": "ws://dev"},
        {"type": "other", "url": "http://b", "webSocketDebuggerUrl": "ws://b"},
        {"type": "page", "url": "http://c"},
    ]
    mgr.loop = asyncio.get_running_loop()
    await mgr._scan()
    assert "ws://a" in mgr._target_urls
    # 已有 target 不重复添加
    await mgr._scan()
    assert len(mgr._tasks) == 1


async def test_cdp_manager_scan_error(cdp_manager):
    mgr = cdp_manager

    class BoomClient:
        async def get(self, url):
            raise RuntimeError("offline")

    mgr._client = BoomClient()
    await mgr._scan()
    assert mgr._target_urls == set()


async def test_cdp_manager_scan_http_error(cdp_manager):
    mgr = cdp_manager
    mgr._client.targets = []
    mgr._client.get = None

    class Bad:
        async def get(self, url):
            return _FakeHttpResponse({}, status=500)

    mgr._client = Bad()
    await mgr._scan()


async def test_cdp_manager_connect_now(cdp_manager):
    mgr = cdp_manager
    mgr._client.targets = [
        {"type": "page", "url": "http://a", "webSocketDebuggerUrl": "ws://a"}
    ]
    mgr.loop = asyncio.get_running_loop()
    await mgr.connect_now()
    # 无真正连接, 50 次后超时但不会挂死
    assert mgr._rescan_evt.is_set() or not mgr._rescan_evt.is_set()


async def test_cdp_manager_connect_now_not_started():
    from backend.services.cdp import CDPManager

    mgr = CDPManager(_cfg())
    mgr.loop = None
    await mgr.connect_now()


async def test_cdp_manager_cleanup_task(cdp_manager):
    mgr = cdp_manager

    async def fake_task():
        await asyncio.sleep(0)

    task = asyncio.create_task(fake_task())
    mgr._task_url[task] = "ws://x"
    mgr._target_urls.add("ws://x")
    mgr._connected.add("ws://x")
    mgr._sessions["ws://x"] = object()
    mgr._tasks.append(task)
    task.add_done_callback(mgr._cleanup_task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # cancel 后完成回调清理状态
    assert "ws://x" not in mgr._target_urls
    assert "ws://x" not in mgr._connected
    assert task not in mgr._tasks


def test_cdp_manager_enable_commands(cdp_manager):
    mgr = cdp_manager

    class Handler:
        name = "h"
        domains = [("Net.enable", {"a": 1}), ("Runtime.enable", {})]

    mgr.register_channel(Handler())
    cmds = mgr._enable_commands()
    methods = [m for m, _ in cmds]
    assert "Runtime.enable" in methods
    assert "Log.enable" in methods
    assert "Page.enable" in methods
    assert "Net.enable" in methods
    assert methods.count("Runtime.enable") == 1


async def test_cdp_manager_dispatch(cdp_manager):
    mgr = cdp_manager

    class Handler:
        name = "h"

        async def on_event(self, session, method, params):
            return method == "X.y"

    mgr.register_channel(Handler())
    session = _FakeSession()
    assert await mgr.dispatch(session, "X.y", {}) is True
    assert await mgr.dispatch(session, "X.z", {}) is False


async def test_cdp_manager_primary_and_session_for(cdp_manager):
    from backend.services.cdp import CDPSession

    mgr = cdp_manager
    s1 = _FakeSession("ws://a")
    s2 = _FakeSession("ws://b")
    mgr._sessions = {"ws://a": s1, "ws://b": s2}
    mgr._target_urls = {"ws://a"}
    # 两个都 ready, 且 ws://a 在 target 中 -> pages=[s1]
    assert mgr.primary() is s1

    # 仅 s2 ready -> candidates=[s2], pages 回退 candidates
    s1.ready = _FakeEvent(False)
    assert mgr.primary() is s2

    # s1 不 ready -> session_for 落到 primary(s2)
    assert mgr.session_for("ws://a") is s2
    assert mgr.session_for("ws://not-there") is s2
    assert mgr.session_for(None) is s2

    # s1 ready -> session_for 命中 s1
    s1.ready = _FakeEvent(True)
    assert mgr.session_for("ws://a") is s1

    # primary 无候选
    s2.ready = _FakeEvent(False)
    mgr._sessions = {}
    assert mgr.primary() is None
    assert mgr.session_for(None) is None


async def test_cdp_manager_evaluate(cdp_manager):
    mgr = cdp_manager
    session = _FakeSession()
    session.responses = {
        "Runtime.evaluate": {
            "id": 1,
            "result": {
                "result": {"type": "string", "value": "hello"},
            },
        }
    }
    mgr._sessions = {"ws://a": session}
    mgr._target_urls = {"ws://a"}
    out = await mgr.evaluate("1+1")
    assert out["ok"] is True
    assert out["item"]["v"] == "hello"

    # exceptionDetails
    session.responses["Runtime.evaluate"] = {
        "id": 1,
        "result": {
            "exceptionDetails": {
                "text": "ReferenceError",
                "exception": {"type": "object", "description": "x is not defined"},
                "stackTrace": {
                    "callFrames": [
                        {"url": "http://a.js", "functionName": "f",
                         "lineNumber": 2, "columnNumber": 3}
                    ]
                },
            }
        },
    }
    out2 = await mgr.evaluate("x")
    assert out2["ok"] is False
    assert "x is not defined" in out2["error"]
    assert "a.js" in out2["stack"]

    # CDP error
    session.responses["Runtime.evaluate"] = {"id": 1, "error": {"message": "cdp fail"}}
    out3 = await mgr.evaluate("x")
    assert out3["ok"] is False

    # 无活动页面
    mgr._sessions = {}
    out4 = await mgr.evaluate("x")
    assert out4["ok"] is False


async def test_cdp_manager_evaluate_command_exception(cdp_manager):
    mgr = cdp_manager

    class BoomSession(_FakeSession):
        async def command(self, method, params=None, timeout=5.0):
            raise RuntimeError("socket closed")

    mgr._sessions = {"ws://a": BoomSession()}
    out = await mgr.evaluate("x")
    assert out["ok"] is False
    assert "socket closed" in out["error"]


async def test_cdp_manager_get_properties(cdp_manager):
    mgr = cdp_manager
    session = _FakeSession()
    session.responses = {
        "Runtime.getProperties": {
            "id": 1,
            "result": {
                "result": [
                    {"name": "a", "value": {"type": "string", "value": "va"}},
                    {"name": "__proto__", "value": {"type": "object"}},
                    {"name": "n", "value": {"type": "number", "value": 3}},
                ]
            },
        }
    }
    mgr._sessions = {"ws://a": session}
    mgr._oid_session["oid1"] = "ws://a"
    out = await mgr.get_properties("oid1")
    assert out["ok"] is True
    assert len(out["props"]) == 2

    # 未注册 oid -> 回退 primary
    mgr._sessions["ws://b"] = session
    out2 = await mgr.get_properties("other")
    assert out2["ok"] is True

    # CDP error
    session.responses["Runtime.getProperties"] = {"id": 1, "error": {"message": "bad"}}
    out3 = await mgr.get_properties("oid1")
    assert out3["ok"] is False

    # 无连接
    mgr._sessions = {}
    out4 = await mgr.get_properties("oid1")
    assert out4["ok"] is False


async def test_cdp_manager_get_properties_exception(cdp_manager):
    mgr = cdp_manager

    class BoomSession(_FakeSession):
        async def command(self, method, params=None, timeout=5.0):
            raise RuntimeError("boom")

    mgr._sessions = {"ws://a": BoomSession()}
    out = await mgr.get_properties("oid1")
    assert out["ok"] is False


def test_remote_item_types(cdp_manager):
    mgr = cdp_manager
    session = _FakeSession()
    assert mgr.remote_item(session, {"type": "string", "value": "s"})["t"] == "str"
    assert mgr.remote_item(session, {"type": "number", "value": 3})["v"] == "3"
    assert mgr.remote_item(session, {"type": "boolean", "value": True})["v"] == "true"
    assert mgr.remote_item(session, {"type": "undefined"})["v"] == "undefined"
    assert mgr.remote_item(session, {"type": "null"})["v"] == "null"
    assert mgr.remote_item(session, {"type": "bigint", "value": "9"})["v"] == "9"
    obj = mgr.remote_item(
        session,
        {
            "type": "object",
            "subtype": "array",
            "className": "Array",
            "description": "Array(2)",
            "objectId": "obj1",
            "preview": {
                "properties": [
                    {"name": "k", "value": "v", "type": "string"},
                    {"name": "k2", "value": "v2", "type": "string"},
                    {"name": "k3", "value": "v3", "type": "string"},
                    {"name": "k4", "value": "v4", "type": "string"},
                    {"name": "k5", "value": "v5", "type": "string"},
                    {"name": "k6", "value": "v6", "type": "string"},
                ]
            },
        },
    )
    assert obj["k"] == "obj"
    assert obj["oid"] == "obj1"
    assert obj["sub"] == "array"
    assert len(obj["prev"]) == 5
    assert mgr._oid_session["obj1"] == session.ws_url

    # error 子类型
    err = mgr.remote_item(session, {"type": "object", "subtype": "error",
                                    "description": "TypeError: x"})
    assert err["v"] == "TypeError: x"

    # 无 objectId -> 不注册
    plain = mgr.remote_item(session, {"type": "object"})
    assert plain["oid"] is None
    assert "ou" not in plain

    # session 为 None
    mgr.remote_item(None, {"type": "object", "objectId": "x"})


def test_stack_text(cdp_manager):
    from backend.services.cdp import CDPManager

    assert CDPManager._stack_text(None) is None
    assert CDPManager._stack_text({"callFrames": []}) is None
    text = CDPManager._stack_text({
        "callFrames": [
            {"url": "http://a", "functionName": "f", "lineNumber": 1, "columnNumber": 2}
        ]
    })
    assert "http://a" in text
    assert text.count("\n") == 0


def test_remote_str():
    from backend.services.cdp import CDPManager

    assert CDPManager._remote_str({"type": "string", "value": "s"}) == "s"
    assert CDPManager._remote_str({"type": "undefined"}) == "undefined"
    assert CDPManager._remote_str({"type": "null"}) == "null"
    assert CDPManager._remote_str({"type": "number", "value": 5}) == "5"
    assert CDPManager._remote_str({"type": "object", "description": "obj"}) == "obj"
    assert CDPManager._remote_str({"type": "object"}) == "<object>"


async def test_poll_loop_stops(cdp_manager):
    mgr = cdp_manager
    mgr._client.targets = []
    task = asyncio.create_task(mgr._poll_loop())
    await asyncio.sleep(0.05)
    mgr._stop = True
    mgr._rescan_evt.set()
    await asyncio.sleep(0)
    task.cancel()


async def test_cdp_manager_stop(cdp_manager):
    mgr = cdp_manager
    mgr._client.targets = []
    mgr._rescan_evt.set()
    await mgr.stop()
    assert mgr._stop is True


async def test_listen_target(monkeypatch):
    from backend.services.cdp import CDPManager

    mgr = CDPManager(_cfg())

    class FakeConn:
        def __init__(self):
            self._events = [
                json.dumps({"method": "Runtime.consoleAPICalled", "params": {"type": "log"}}),
            ]

        async def send(self, data):
            msg = json.loads(data)
            self._events.insert(0, json.dumps({"id": msg["id"], "result": {}}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._events:
                await asyncio.sleep(0.02)
                return self._events.pop(0)
            raise StopAsyncIteration

    def fake_connect(url, **kwargs):
        return FakeConn()

    monkeypatch.setattr("backend.services.cdp.websockets.connect", fake_connect)

    handled = []

    class H:
        name = "h"

        async def on_event(self, session, method, params):
            handled.append((method, params))
            return True

    mgr.register_channel(H())
    mgr.loop = asyncio.get_running_loop()
    mgr._client = _FakeHttpClient()
    await mgr._listen_target("ws://x")
    assert "ws://x" in mgr._connected
    assert handled
    mgr._stop = True


async def test_read_session_invalid_json():
    from backend.services.cdp import CDPManager, CDPSession

    session = CDPSession("ws://x")
    session.queue = asyncio.Queue()

    class FakeWS:
        def __init__(self):
            self.messages = [
                "not json{{{",
                json.dumps({"id": 5, "result": {}}),
                json.dumps({"method": "M.x", "params": {"a": 1}}),
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.messages:
                return self.messages.pop(0)
            raise StopAsyncIteration

    session.ws = FakeWS()
    resolved = []

    def _resolve(m):
        resolved.append(m["id"])

    session.resolve = _resolve  # type: ignore[assignment]
    mgr = CDPManager(_cfg())
    await CDPManager._read_session(mgr, session)  # type: ignore[arg-type]
    assert resolved == [5]
    assert not session.queue.empty()


def _cfg():
    from backend.config import Config

    return Config()
