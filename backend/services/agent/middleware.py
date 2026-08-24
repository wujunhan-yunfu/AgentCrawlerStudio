"""Agent 事件中间件: 把模型调用/工具调用过程实时推送到前端。

基于 langchain.agents.middleware.AgentMiddleware 实现:
- aafter_model:  模型回复后推送 tool 调用 / message_end 事件
- awrap_tool_call: 工具执行后推送 tool_result 事件
- abefore_agent / aafter_agent: 会话生命周期事件

事件不承载大内容(均做了截断), 实时增量(token)由 runner 通过
stream_mode="messages" 单独转发为 delta 事件。
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from .core.text import cap_text
from .session.model import AgentSession

_MAX_RESULT = 4000
_MAX_ARGS = 1500


class AgentEventMiddleware(AgentMiddleware[Any, Any, Any]):
    """把 agent 推理过程转发到会话事件总线。"""

    def __init__(self, session: AgentSession):
        self._session = session
        self._name = "AgentEventMiddleware"

    @property
    def name(self) -> str:
        return self._name

    async def abefore_agent(
        self, state: Any, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        self._session.emit(
            {"type": "status", "content": "已开始处理任务, 正在分析需求..."}
        )
        return None

    async def aafter_model(
        self, state: Any, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage):
            return None
        if last.content:
            self._session.emit({"type": "message_end"})
        for call in last.tool_calls:
            raw_args = call.get("args") or {}
            if isinstance(raw_args, str):
                args: str = raw_args
            else:
                try:
                    args = json.dumps(raw_args, ensure_ascii=False)
                except (TypeError, ValueError):
                    args = str(raw_args)
            self._session.emit(
                {
                    "type": "tool",
                    "name": call.get("name"),
                    "args": cap_text(args, _MAX_ARGS),
                    "id": call.get("id"),
                }
            )
        return None

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        name = request.tool_call.get("name")
        result = await handler(request)
        content = ""
        error = ""
        if isinstance(result, Command):
            content = "命令已执行"
        else:
            text = getattr(result, "content", None)
            if text is not None:
                content = cap_text(text, _MAX_RESULT)
            status = getattr(result, "status", None)
            if status == "error":
                error = content
        self._session.emit(
            {
                "type": "tool_result",
                "name": name,
                "id": request.tool_call.get("id"),
                "content": content,
                "error": error,
            }
        )
        return result

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        # 非工具型思考中状态, 让前端显示"思考中"而不打断消息流
        self._session.emit({"type": "status", "content": "思考中..."})
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001
            self._session.emit({"type": "status", "content": f"模型调用失败: {exc}"})
            raise
