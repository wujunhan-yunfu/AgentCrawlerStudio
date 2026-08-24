"""backend.services.agent.tools 与 backend.services.agent.agent 测试。"""

from __future__ import annotations

import pytest

from conftest import FakeStream
from backend.services.agent.session.event import EventHub
from backend.services.agent.session.model import AgentSession, EditorState


def make_session():
    return AgentSession(id="t1", crawler_id="c", title="T", hub=EventHub())


# --------------------------------------------------------------------------- build_tools


async def test_build_tools_returns_all():
    from backend.services.agent.bridge import BrowserBridge
    from backend.services.agent.tools import build_tools

    session = make_session()
    bridge = BrowserBridge(FakeStream())
    tools = build_tools(session, bridge, editor=EditorState())
    names = sorted(t.name for t in tools)
    assert "archive_content" in names
    assert "ask_user" in names
    assert "record_plan" in names
    assert "browser_navigate" in names
    assert "browser_pages" in names
    assert "browser_evaluate" in names
    assert "page_analyze" in names
    assert "browser_run_code" in names
    assert "http_request" in names
    assert "get_editor_code" in names
    assert "debug_code" in names
    assert "set_editor_code" in names
    assert len(tools) == 12


# --------------------------------------------------------------------------- build_agent


def test_build_agent(monkeypatch):
    from backend.services.agent import agent as agent_mod

    cfg = type("Cfg", (), {"llm_api_key": "sk-x"})()
    session = make_session()
    bridge = type("B", (), {})()
    calls = {}
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda cfg: "MODEL")
    monkeypatch.setattr(
        agent_mod, "build_tools", lambda s, b, editor=None: ["TOOL"]
    )
    monkeypatch.setattr(agent_mod, "get_checkpointer", lambda cfg: "CHECKPOINTER")
    monkeypatch.setattr(
        agent_mod,
        "create_deep_agent",
        lambda **kw: (calls.update(kw), kw)[1],
    )
    result = agent_mod.build_agent(cfg, session, bridge, editor=None)
    assert result["model"] == "MODEL"
    assert result["tools"] == ["TOOL"]
    assert result["checkpointer"] == "CHECKPOINTER"
    assert result["system_prompt"] == agent_mod.SYSTEM_PROMPT
    assert result["backend"] is agent_mod.AGENT_BACKEND
    assert result["name"] == "crawler-agent"
    assert len(result["middleware"]) == 3
