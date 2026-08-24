"""会话状态模型: 可多轮问答的会话容器与前端编辑器镜像。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .event import EventHub


@dataclass
class AgentSession:
    """一个可多轮对话的会话: 会话元信息 + 当前轮次执行状态。"""

    id: str
    crawler_id: str
    title: str
    hub: EventHub
    created_at: float = field(default_factory=time.time)
    status: str = "idle"  # idle / running / waiting / done / error / cancelled
    plan: dict[str, Any] | None = None
    question: dict[str, Any] | None = None
    answer_future: asyncio.Future | None = None
    # 登录协作(page_login): 挂起时记录登录载荷与等待的 future, 供 /login-action 与 /login-answer
    login: dict[str, Any] | None = None
    login_future: asyncio.Future | None = None
    # 执行事件持久化回调(由 runner 注入): persist(session, event) -> None
    persist: Any = None
    task_handle: asyncio.Task | None = None
    # 会话落库与 Agent 预构建的后台任务(创建会话时即启动, 不阻塞请求响应)
    persist_task: Any = None
    build_task: Any = None
    agent: Any = None
    config: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # 本轮进程内是否已跑过(决定续聊时是否需要先注入历史)
    started: bool = False
    # 标题是否由用户手动修改: 手动修改后不再被自动生成的标题覆盖
    title_manual: bool = False

    def emit(self, event: dict[str, Any]) -> None:
        event.setdefault("session_id", self.id)
        event.setdefault("crawler_id", self.crawler_id)
        self.hub.emit(event)


class EditorState:
    """前端编辑器代码的后端镜像: Agent 可读取/回写, 前端定时同步。

    base_code 记录「本轮修改开始前」的编辑器内容(源文件基线),
    用于前端展示「较源文件」的总变更。
    """

    def __init__(self) -> None:
        self.code: str = ""
        self.base_code: str = ""

    def get(self) -> str:
        return self.code

    def set(self, code: str) -> None:
        self.code = code

    def mark_turn(self) -> None:
        """新一轮对话开始时, 把当前编辑器内容记为变更基线。"""
        self.base_code = self.code
