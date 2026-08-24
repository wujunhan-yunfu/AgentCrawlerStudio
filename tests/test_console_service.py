"""backend.services.console (ConsoleChannel) 测试。"""

from __future__ import annotations

import asyncio
import json

import pytest


def _make_channel(fake_cdp_mgr):
    from backend.services.console import ConsoleChannel

    ch = ConsoleChannel(fake_cdp_mgr)
    return ch


async def _collect(channel, timeout=0.5):
    sub = await channel.channel.attach()
    items = []
    while True:
        try:
            item = await asyncio.wait_for(sub.wait(timeout=0.05), timeout=0.05)
        except asyncio.TimeoutError:
            break
        if item is None:
            continue
        items.append(json.loads(item))
        if len(items) >= 3:
            break
    await channel.channel.detach(sub)
    return items


async def test_on_event_unknown(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    assert await ch.on_event(fake_cdp_mgr.session, "Foo.bar", {}) is False


async def test_on_event_console_api(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    assert await ch.on_event(fake_cdp_mgr.session, "Runtime.consoleAPICalled", {
        "type": "log",
        "args": [{"type": "string", "value": "hi"}],
    }) is True


async def test_console_api_playwright_marker(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = await ch._console_api(fake_cdp_mgr.session, {
        "type": "log",
        "args": [{"type": "string", "value": "--playwright-- done"}],
    })
    assert msg is None


async def test_console_api_clear(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = await ch._console_api(fake_cdp_mgr.session, {"type": "clear", "args": []})
    assert msg["kind"] == "clear"


async def test_console_api_group_flow(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = await ch._console_api(fake_cdp_mgr.session, {
        "type": "startGroup", "args": [{"type": "string", "value": "grp"}],
    })
    assert msg["kind"] == "startGroup"
    assert fake_cdp_mgr.session.group_depth == 1
    end = await ch._console_api(fake_cdp_mgr.session, {"type": "endGroup", "args": []})
    assert end["kind"] == "groupEnd"
    assert fake_cdp_mgr.session.group_depth == 0
    # 重复 endGroup 不越界
    await ch._console_api(fake_cdp_mgr.session, {"type": "endGroup", "args": []})
    assert fake_cdp_mgr.session.group_depth == 0


async def test_console_api_assert(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = await ch._console_api(fake_cdp_mgr.session, {
        "type": "assert",
        "args": [{"type": "boolean", "value": False}, {"type": "string", "value": "bad"}],
    })
    assert msg["kind"] == "assert"
    assert msg["level"] == "error"
    # 无文本 -> 默认文案
    msg2 = await ch._console_api(fake_cdp_mgr.session, {
        "type": "assert", "args": [{"type": "boolean", "value": False}],
    })
    assert msg2["items"][0]["v"] == "Assertion failed"


async def test_console_api_trace(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = await ch._console_api(fake_cdp_mgr.session, {
        "type": "trace", "args": [{"type": "string", "value": "tr"}],
    })
    assert msg["kind"] == "trace"


async def test_console_api_table(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)

    class Session:
        group_depth = 0
        ws_url = "ws://fake"

        async def command(self, method, params=None, timeout=5.0):
            assert method == "Runtime.callFunctionOn"
            return {"id": 1, "result": {
                "result": {"type": "string", "value": "[{\"a\":1}]"},
                "exceptionDetails": None,
            }}

    msg = await ch._console_api(Session(), {
        "type": "table",
        "args": [{"type": "object", "objectId": "o1"}],
    })
    assert msg["table"] == [{"a": 1}]


async def test_console_api_table_bad_json(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)

    class Session:
        group_depth = 0
        ws_url = "ws://fake"

        async def command(self, method, params=None, timeout=5.0):
            return {"id": 1, "result": {"result": {"type": "string", "value": "not json"}}}

    msg = await ch._console_api(Session(), {
        "type": "table", "args": [{"type": "object", "objectId": "o1"}],
    })
    assert msg.get("table") is None


async def test_stringify_exception_and_no_value(fake_cdp_mgr):
    from backend.services.console import ConsoleChannel

    ch = _make_channel(fake_cdp_mgr)

    class Session:
        async def command(self, method, params=None, timeout=5.0):
            return {"id": 1, "result": {"exceptionDetails": {"text": "x"}}}

    assert await ch._stringify(Session(), "o1") is None

    class Session2:
        async def command(self, method, params=None, timeout=5.0):
            return {"id": 1, "result": {"result": {"type": "number", "value": 5}}}

    assert await ch._stringify(Session2(), "o1") is None

    class Session3:
        async def command(self, method, params=None, timeout=5.0):
            raise RuntimeError("boom")

    assert await ch._stringify(Session3(), "o1") is None


async def test_console_api_regular_specifiers(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = await ch._console_api(fake_cdp_mgr.session, {
        "type": "log",
        "args": [
            {"type": "string", "value": "%s = %d, %i, %f, %o, %%, %c styled %s"},
            {"type": "string", "value": "abc"},
            {"type": "number", "value": 42},
            {"type": "number", "value": 7},
            {"type": "number", "value": 3.14},
            {"type": "object", "description": "Object", "objectId": "o"},
            {"type": "string", "value": "color:red"},
            {"type": "string", "value": "tail"},
            {"type": "number", "value": 99},
        ],
    })
    text = msg["text"]
    assert "abc" in text
    assert "42" in text
    assert "7" in text
    assert "3.14" in text
    assert "%" in text
    assert "tail" in text
    # 格式化 %o 对象保留样式/尾部
    assert any(s.get("style") == "color:red" for s in msg["items"])


async def test_console_api_format_invalid(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = await ch._console_api(fake_cdp_mgr.session, {
        "type": "log",
        "args": [{"type": "string", "value": "%d %d"},
                 {"type": "string", "value": "not-a-number"}],
    })
    assert "NaN" in msg["text"]


async def test_console_api_no_args(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    assert await ch._console_api(fake_cdp_mgr.session, {"type": "log", "args": []}) is not None
    assert await ch._console_api(fake_cdp_mgr.session, {"type": "log"}) is not None


async def test_console_api_first_arg_non_string(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = await ch._console_api(fake_cdp_mgr.session, {
        "type": "log",
        "args": [{"type": "number", "value": 5}, {"type": "boolean", "value": True}],
    })
    assert msg["items"][0]["v"] == "5"


def test_exception_thrown(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = ch._exception_thrown({
        "timestamp": 1234567890000,
        "exceptionDetails": {
            "exception": {"type": "object", "subtype": "error",
                          "className": "TypeError", "description": "TypeError: bad"},
            "url": "http://x/app.js",
            "lineNumber": 2,
            "stackTrace": {"callFrames": [{"url": "http://x/app.js", "functionName": "f"}]},
        },
    })
    assert msg["kind"] == "exception"
    assert msg["level"] == "error"
    assert msg["ts"] < 1e11
    assert "TypeError: bad" in msg["items"][0]["v"]


def test_exception_thrown_fallbacks(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = ch._exception_thrown({"exceptionDetails": {"text": "Uncaught"}})
    assert msg["items"][0]["v"] == "Uncaught"
    msg2 = ch._exception_thrown({"exceptionDetails": {}})
    assert msg2["items"][0]["v"] == "Uncaught exception"
    msg3 = ch._exception_thrown({"exceptionDetails": {
        "exception": {"type": "object", "objectId": "o", "subtype": "error"}}})
    assert "<error>" in msg3["items"][0]["v"]


def test_log_entry(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = ch._log_entry({
        "timestamp": 1234,
        "entry": {"level": "warning", "source": "javascript", "text": "warn msg",
                  "url": "u", "lineNumber": 1},
    })
    assert msg["kind"] == "entry"
    assert msg["level"] == "warning"
    assert msg["items"][0]["v"] == "[javascript] warn msg"
    # 未知级别 -> log
    msg2 = ch._log_entry({"entry": {"level": "nope", "source": "", "text": "t"}})
    assert msg2["level"] == "log"


def test_make_message_ts(fake_cdp_mgr):
    ch = _make_channel(fake_cdp_mgr)
    msg = ch._make_message("x", "log", [{"k": "text", "v": "a"}], line=4, ts=1000.0)
    assert msg["line"] == 5
    assert msg["ts"] == 1000.0
    # 毫秒时间戳换算
    msg2 = ch._make_message("x", "log", [], ts=1e12)
    assert msg2["ts"] < 1e11
    # 无时间戳 -> 用当前时间
    msg3 = ch._make_message("x", "log", [])
    assert msg3["ts"] > 0
    # table 参数
    msg4 = ch._make_message("x", "log", [], table=[[1]])
    assert msg4["table"] == [[1]]


def test_segments_text(fake_cdp_mgr):
    from backend.services.console import ConsoleChannel

    assert ConsoleChannel._segments_text([{"v": "a"}, {"v": "b"}]) == "ab"
    assert ConsoleChannel._text_segment("x") == {"k": "text", "t": "str", "v": "x"}


def test_spec_value():
    from backend.services.console import ConsoleChannel

    assert ConsoleChannel._spec_value("s", {"value": "sv"}) == "sv"
    assert ConsoleChannel._spec_value("d", {"value": "3.9"}) == "3"
    assert ConsoleChannel._spec_value("i", {"value": 5}) == "5"
    assert ConsoleChannel._spec_value("f", {"value": 2.5}) == "2.5"
    # 非法 -> NaN
    assert ConsoleChannel._spec_value("d", {"value": "abc"}) == "NaN"
    assert ConsoleChannel._spec_value("f", {"value": "abc"}) == "NaN"
    # 未知 spec 返回空
    assert ConsoleChannel._spec_value("x", {"value": 1}) == ""
