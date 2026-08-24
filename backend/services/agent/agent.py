"""爬虫 Agent 组装: 基于 deepagents.create_deep_agent 构建统一 Agent。

不再区分「爬虫采集」与「编码调试」两种类型: 意图由 Agent 自行判断。
无论用户是要采集数据, 还是要修改/优化编辑器里的脚本, 最终都以
「把完整可复用的脚本写回编辑器」为交付目标。

系统提示词独立存放在 prompts/system.md, 由 prompts 包加载。
"""

from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, TodoListMiddleware

from .bridge import BrowserBridge
from .checkpointer import get_checkpointer
from .core.fs import AGENT_BACKEND
from .core.llm import build_chat_model
from .middleware import AgentEventMiddleware
from .prompts import SYSTEM_PROMPT
from .session.model import AgentSession
from .tools import build_tools


def build_agent(
    cfg: Any, session: AgentSession, bridge: BrowserBridge, editor: Any = None
) -> Any:
    """构建统一 Agent(每个会话独立构建, 便于绑定会话事件与独立 checkpointer)。

    编辑器读写工具始终可用: Agent 自行判断意图, 最终都会写回/优化编辑器脚本。
    """
    model = build_chat_model(cfg)
    tools = build_tools(session, bridge, editor=editor)
    checkpointer = get_checkpointer(cfg)
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=[
            AgentEventMiddleware(session),
            TodoListMiddleware(system_prompt=""),
            ModelCallLimitMiddleware(
                thread_limit=160, run_limit=160, exit_behavior="end"
            ),
        ],
        backend=AGENT_BACKEND,
        checkpointer=checkpointer,
        name="crawler-agent",
    )
