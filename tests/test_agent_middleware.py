"""backend.services.agent.middleware.AgentEventMiddleware 测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
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
    # message_end 需携带本轮文本, 供前端落库还原「调用工具前的过渡文字」
    end = [e for e in session.hub._buffer if e["type"] == "message_end"][-1]
    assert end["content"] == "回答"


async def test_message_end_content_blocks():
    """多模态 content block 列表归一为纯文本。"""
    mw, session = make_middleware()
    aim = AIMessage(content="")
    aim.content = [{"type": "text", "text": "第一段"}, "拼接", {"type": "text", "text": "第二段"}]
    await mw.aafter_model({"messages": [aim]}, None)
    end = [e for e in session.hub._buffer if e["type"] == "message_end"][-1]
    assert end["content"] == "第一段拼接第二段"


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


# ---------------------------------------------------------------- 过程叙述提醒


def make_request(messages, system_text: str | None = "系统提示"):
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import SystemMessage

    return ModelRequest(
        model=None,
        messages=messages,
        system_message=SystemMessage(content=system_text) if system_text else None,
        state={"messages": messages},
        runtime=None,
    )


async def test_narration_after_tool_result():
    """工具结果返回后, 注入"先分析结果再继续"的临时系统提醒。"""
    mw, _ = make_middleware()
    messages = [
        HumanMessage(content="采集数据"),
        AIMessage(
            content="先分析页面结构。",
            tool_calls=[{"name": "page_analyze", "args": {}, "id": "1"}],
        ),
        ToolMessage(content="分析结果", tool_call_id="1"),
    ]
    req = make_request(messages)
    out = mw._with_narration_reminder(req)
    assert "过程叙述" in out.system_message.content
    assert "工具结果" in out.system_message.content
    # 原请求不被修改, 对话状态也不受影响
    assert "过程叙述" not in req.system_message.content


async def test_narration_silent_tool_turn():
    """上一批工具调用没有可见文本时, 使用更强的提醒。"""
    mw, _ = make_middleware()
    messages = [
        HumanMessage(content="采集数据"),
        AIMessage(
            content="", tool_calls=[{"name": "browser_run_code", "args": {}, "id": "1"}]
        ),
        ToolMessage(content="ok", tool_call_id="1"),
    ]
    out = mw._with_narration_reminder(make_request(messages))
    assert "没有给用户任何说明" in out.system_message.content


async def test_narration_no_reminder_for_user_turn():
    mw, _ = make_middleware()
    req = make_request([HumanMessage(content="你好")])
    assert mw._with_narration_reminder(req) is req


async def test_narration_without_system_message_prepends():
    """无系统消息时, 临时系统提醒插到消息最前面, 不丢原有消息。"""
    mw, _ = make_middleware()
    messages = [
        AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}]),
        ToolMessage(content="ok", tool_call_id="1"),
    ]
    out = mw._with_narration_reminder(make_request(messages, system_text=None))
    assert out.system_message is None
    assert isinstance(out.messages[0], SystemMessage)
    assert out.messages[1:] == messages


async def test_narration_merges_system_message_in_messages():
    """系统消息已在 messages 中时合并进去, 不再额外插入一条。"""
    mw, _ = make_middleware()
    messages = [
        SystemMessage(content="系统提示"),
        AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}]),
        ToolMessage(content="ok", tool_call_id="1"),
    ]
    out = mw._with_narration_reminder(make_request(messages, system_text=None))
    assert len(out.messages) == len(messages)
    assert out.messages[0].content.startswith("系统提示")
    assert "过程叙述" in out.messages[0].content


async def test_awrap_model_call_injects_reminder():
    mw, _ = make_middleware()
    messages = [
        AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}]),
        ToolMessage(content="ok", tool_call_id="1"),
    ]
    req = make_request(messages)
    seen = {}

    async def handler(r):
        seen["req"] = r
        return SimpleNamespace()

    await mw.awrap_model_call(req, handler)
    assert "过程叙述" in seen["req"].system_message.content
