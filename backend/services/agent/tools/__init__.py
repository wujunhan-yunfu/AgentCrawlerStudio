"""爬虫 Agent 工具集: 按职责拆分并统一组装。

- browser.py:   浏览器控制(导航/分析/求值/截图/整体运行)
- http.py:      HTTP 探测
- save.py:      内容归档
- planning.py:  规划与问卷
- editor.py:    编辑器读写与临时调试

工具基于 langchain_core.tools 构建, 供 deepagents 的 create_deep_agent 装配。
其中 record_plan 与 ask_user 需要访问当前会话, 由 build_tools(session, bridge) 工厂创建。
"""

from __future__ import annotations

from ..bridge import BrowserBridge
from ..session.model import AgentSession, EditorState
from .browser import build_browser_tools
from .editor import build_editor_tools
from .http import build_http_tools
from .planning import build_planning_tools
from .save import build_save_tools


def build_tools(
    session: AgentSession, bridge: BrowserBridge, editor: EditorState | None = None
) -> list:
    """按会话创建工具列表, 始终包含编辑器读写工具(统一 Agent 最终都要写回编辑器)。"""
    tools: list = []
    tools += build_browser_tools(session, bridge)
    tools += build_http_tools()
    tools += build_save_tools(session)
    tools += build_planning_tools(session)
    tools += build_editor_tools(session, bridge, editor)
    return tools


__all__ = ["build_tools"]
