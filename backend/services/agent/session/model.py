"""会话状态模型: 可多轮问答的会话容器与前端编辑器镜像。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.text import cap_text
from .event import PERSISTED_EVENT_TYPES, EventHub


@dataclass
class AgentSession:
    """一个可多轮对话的会话: 会话元信息 + 当前轮次执行状态。"""

    id: str
    crawler_id: str
    title: str
    hub: EventHub
    created_at: float = field(default_factory=time.time)
    status: str = "idle"  # idle / running / waiting / done / error / cancelled / interrupted
    plan: dict[str, Any] | None = None
    question: dict[str, Any] | None = None
    answer_future: asyncio.Future | None = None
    # 登录协作(page_login): 挂起时记录登录载荷与等待的 future, 供 /login-action 与 /login-answer
    login: dict[str, Any] | None = None
    login_future: asyncio.Future | None = None
    # 执行事件持久化回调(由 runner 注入): persist(session, event) -> None
    persist: Any = None
    # 持久化 FIFO 队列与消费任务(由 runner 在有事件循环时启动):
    # emit 的事件先实时广播, 再入队顺序落库, 保证历史记录顺序与执行顺序一致。
    persist_queue: Any = None
    persist_worker: Any = None
    # 最近一条已落库的 assistant 文本, 用于避免 _finalize_done 重复写入最终回答
    last_assistant_text: str = ""
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
        if event.get("type") == "saved" and isinstance(event.get("saved"), list):
            # 事件只保留预览: 完整产物通过下载/详情读取, 避免 WS/Mongo 被大内容撑爆。
            preview = []
            for item in event["saved"]:
                if isinstance(item, dict):
                    preview.append(
                        {**item, "content": cap_text(item.get("content", ""), 2000)}
                    )
                else:
                    preview.append({"content": cap_text(item, 2000)})
            event["saved"] = preview
        self.hub.emit(event)
        self._enqueue_persist(event)

    def _enqueue_persist(self, event: dict[str, Any]) -> None:
        """把需要留痕的事件放入持久化队列(实时广播不受影响)。"""
        queue = self.persist_queue
        if queue is None or self.persist is None:
            return
        if event.get("type") not in PERSISTED_EVENT_TYPES:
            return
        try:
            queue.put_nowait(dict(event))
        except asyncio.QueueFull:
            pass


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
