"""backend.services.agent.middleware.AgentEventMiddleware 测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from backend.services.agent.middleware import AgentEventMiddleware
from backend.services.agent.session.event import EventHub
from backend.services.agent.session.model import AgentSession


def make_middleware():
    session = AgentSession(id="m1", crawler_id="c", title="T", hub=EventHub())
    return AgentEventMiddleware(session), session


async def test_name():
    mw, _ = make_middleware()
    assert mw.name == "AgentEventMiddleware"


async def test_abefore_agent():
    mw, session = make_middleware()
    out = await mw.abefore_agent({}, None)
    assert out is None
    assert session.hub._buffer[-1]["type"] == "status"


async def test_aafter_model_no_messages():
    mw, _ = make_middleware()
    assert await mw.aafter_model({"messages": []}, None) is None


async def test_aafter_model_not_aimessage():
    mw, _ = make_middleware()
    assert (
        await mw.aafter_model({"messages": [HumanMessage(content="x")]}, None) is None
    )


async def test_aafter_model_content_and_tool_calls():
    mw, session = make_middleware()
    aim = AIMessage(
        content="回答", tool_calls=[{"name": "t1", "args": {"a": 1}, "id": "1"}]
    )
    await mw.aafter_model({"messages": [aim]}, None)
    types = [e["type"] for e in session.hub._buffer]
    assert "message_end" in types
    assert "tool" in types
    tool = [e for e in session.hub._buffer if e["type"] == "tool"][-1]
    assert tool["name"] == "t1"


async def test_aafter_model_string_args():
    mw, session = make_middleware()
    aim = AIMessage(content="", tool_calls=[{"name": "t2", "args": {}, "id": "2"}])
    aim.tool_calls[0]["args"] = "raw-string"  # 绕过 pydantic 校验, 模拟字符串 args
    await mw.aafter_model({"messages": [aim]}, None)
    tool = [e for e in session.hub._buffer if e["type"] == "tool"][-1]
    assert tool["args"] == "raw-string"


async def test_aafter_model_unserializable_args():
    mw, session = make_middleware()
    aim = AIMessage(content="", tool_calls=[{"name": "t3", "args": {}, "id": "3"}])
    aim.tool_calls[0]["args"] = SimpleNamespace(no="json")  # json.dumps 失败
    await mw.aafter_model({"messages": [aim]}, None)
    tool = [e for e in session.hub._buffer if e["type"] == "tool"][-1]
    assert "namespace(" in tool["args"]


async def test_awrap_tool_call_content_capped():
    mw, session = make_middleware()
    req = SimpleNamespace(tool_call={"name": "t", "id": "1"})

    async def handler(request):
        return ToolMessage(content="x" * 10000, tool_call_id="1")

    result = await mw.awrap_tool_call(req, handler)
    assert isinstance(result, ToolMessage)
    tr = [e for e in session.hub._buffer if e["type"] == "tool_result"][-1]
    assert "已截断" in tr["content"]


async def test_awrap_tool_call_error_status():
    mw, session = make_middleware()
    req = SimpleNamespace(tool_call={"name": "t", "id": "1"})

    async def handler(request):
        return SimpleNamespace(content="报错信息", status="error")

    await mw.awrap_tool_call(req, handler)
    tr = [e for e in session.hub._buffer if e["type"] == "tool_result"][-1]
    assert tr["error"] == "报错信息"


async def test_awrap_tool_call_command():
    mw, session = make_middleware()
    req = SimpleNamespace(tool_call={"name": "t", "id": "1"})

    async def handler(r):
        return Command(resume={"x": 1})

    result = await mw.awrap_tool_call(req, handler)
    assert isinstance(result, Command)
    tr = [e for e in session.hub._buffer if e["type"] == "tool_result"][-1]
    assert tr["content"] == "命令已执行"


async def test_awrap_model_call_ok():
    mw, session = make_middleware()

    async def handler(r):
        return SimpleNamespace()

    result = await mw.awrap_model_call(None, handler)
    assert session.hub._buffer[-1]["content"] == "思考中..."


async def test_awrap_model_call_error():
    mw, session = make_middleware()

    async def bad(request):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await mw.awrap_model_call(None, bad)
    assert "模型调用失败" in session.hub._buffer[-1]["content"]
