"""爬虫 Agent: 基于 langchain / deepagents 的会话式多轮对话 Agent。

- POST /agent/session              新建会话(可多轮对话)
- GET  /agent/sessions             当前 crawler_id 的会话列表
- POST /agent/session/<id>/message 向会话发送消息, 驱动一轮对话
- GET  /agent/session/<id>/messages 读取会话消息历史
- DELETE /agent/session/<id>       删除会话
- WS   /ws/agent                   实时推送执行事件(消息/任务清单/规划/问卷/工具调用)
- POST /agent/answer               提交问卷答案(Agent 无法抉择时弹出)
- POST /agent/stop                 停止当前轮次
- GET  /agent/info                 后端配置的 crawler_id

会话/消息按 crawler_id 隔离并持久化到 MongoDB。

包结构(按依赖分层):
- core/:      基础层(LLM 构建 / 虚拟文件系统路径 / 文本工具)
- session/:   会话层(事件总线 / 会话模型 / MongoDB 持久化)
- prompts/:   提示词独立存放
- tools/:     工具层(浏览器 / HTTP / 归档 / 规划 / 编辑器)
- bridge.py:  浏览器桥接(依赖 core)
- middleware.py: 事件中间件(依赖 session)
- agent.py:   Agent 组装(依赖以上各层)
- runner.py:  Agent 管理编排(入口)
"""

from __future__ import annotations

from .core.llm import resolve_base_url
from .prompts import SYSTEM_PROMPT
from .runner import AgentManager

__all__ = [
    "AgentManager",
    "SYSTEM_PROMPT",
    "resolve_base_url",
]
