"""Agent 事件中间件: 把模型调用/工具调用过程实时推送到前端。

基于 langchain.agents.middleware.AgentMiddleware 实现:
- aafter_model:  模型回复后推送 tool 调用 / message_end 事件
- awrap_tool_call: 工具执行后推送 tool_result 事件
- awrap_model_call: 在工具结果之后补一条"过程叙述"提醒, 引导模型先分析结果、再说明
  下一次工具调用的意图(仅注入本次请求, 不写入对话状态)
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
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from .core.text import cap_text, content_text
from .session.model import AgentSession

_MAX_RESULT = 4000
_MAX_ARGS = 1500

# 过程叙述兜底: 在工具结果之后、以及模型跳过叙述直接调用工具时, 为本次请求临时补一条
# 系统提醒。只注入本次请求, 不写入对话状态, 避免污染历史/持续占用上下文。
_ANALYSIS_REMINDER = (
    "\n\n[过程叙述] 你刚收到工具结果。请先用 1~2 句说明: 结果说明了什么、是否符合预期、"
    "下一步怎么做; 若还要调用工具, 再说明本次操作的意图与目的。不要跳过分析直接连续调用工具。"
)

_SILENT_REMINDER = (
    "\n\n[过程叙述] 你上一批工具调用前没有给用户任何说明。请先用 1~2 句自然语言分析上一步"
    "工具结果(发现了什么/说明什么问题/下一步怎么做), 并在调用下一个工具前说明本次操作的"
    "意图与目的; 只写有信息量的结论, 不写套话。"
)


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
            # 携带文本: 前端据此结束流式气泡, 同时落库为该轮 assistant 消息
            # (含调用工具前输出的过渡文字, 刷新/切换会话后可还原)。
            self._session.emit(
                {"type": "message_end", "content": content_text(last.content)}
            )
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
        try:
            result = await handler(request)
        except Exception as exc:
            # 工具异常也必须发 tool_result, 否则前端工具卡片会一直停在 running。
            content = f"{type(exc).__name__}: {exc}"
            self._session.emit(
                {
                    "type": "tool_result",
                    "name": name,
                    "id": request.tool_call.get("id"),
                    "content": cap_text(content, _MAX_RESULT),
                    "error": cap_text(content, _MAX_RESULT),
                }
            )
            raise
        content = ""
        error = ""
        has_image = False
        if isinstance(result, Command):
            content = "命令已执行"
        else:
            raw = getattr(result, "content", None)
            if raw is not None:
                if isinstance(raw, list):
                    # 多模态工具(如 browser_screenshot): 只把文本块发给前端/落库,
                    # 图片 base64 不进入事件, 避免 WS/Mongo 被 data URI 撑爆。
                    parts = []
                    for block in raw:
                        if isinstance(block, dict) and block.get("type") == "image_url":
                            has_image = True
                        elif isinstance(block, str):
                            parts.append(block)
                        elif isinstance(block, dict) and isinstance(block.get("text"), str):
                            parts.append(block["text"])
                    content = cap_text("".join(parts), _MAX_RESULT)
                else:
                    content = cap_text(raw, _MAX_RESULT)
            status = getattr(result, "status", None)
            if status == "error":
                error = content
        event = {
            "type": "tool_result",
            "name": name,
            "id": request.tool_call.get("id"),
            "content": content,
            "error": error,
        }
        if has_image:
            event["has_image"] = True
        self._session.emit(event)
        return result

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        # 工具结果之后 / 上一批调用无叙述时, 本次请求临时补一条过程叙述提醒
        request = self._with_narration_reminder(request)
        # 非工具型思考中状态, 让前端显示"思考中"而不打断消息流
        self._session.emit({"type": "status", "content": "思考中..."})
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001
            self._session.emit({"type": "status", "content": f"模型调用失败: {exc}"})
            raise

    @staticmethod
    def _tool_turn_was_silent(messages: list[Any]) -> bool:
        """上一批工具调用是否完全没有可见文本(既未说明意图, 也未分析结果)。"""
        idx = len(messages) - 1
        # 工具结果之后: 回退到真正发起这批调用的 AIMessage
        while idx >= 0 and isinstance(messages[idx], ToolMessage):
            idx -= 1
        if idx < 0:
            return False
        last = messages[idx]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return False
        return not content_text(last.content).strip()

    def _narration_reminder(self, messages: list[Any]) -> str | None:
        """按对话当前位置给出过程叙述提醒(无需提醒时返回 None)。"""
        if not messages:
            return None
        if isinstance(messages[-1], ToolMessage):
            # 工具结果刚返回: 提醒模型先分析结果再决定下一步
            return (
                _SILENT_REMINDER
                if self._tool_turn_was_silent(messages)
                else _ANALYSIS_REMINDER
            )
        if self._tool_turn_was_silent(messages):
            # 上一批调用没说明意图: 下次调用前补上叙述
            return _SILENT_REMINDER
        return None

    def _with_narration_reminder(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        """给工具执行过程补一条一次性系统提醒(不改动对话状态)。"""
        if request is None:
            return request
        messages = list(getattr(request, "messages", None) or [])
        reminder = self._narration_reminder(messages)
        if not reminder:
            return request
        system_message = getattr(request, "system_message", None)
        override = getattr(request, "override", None)
        if override is None:
            return request
        if system_message is None:
            # 无独立系统消息时, 优先合并进 messages 中已有的系统消息, 避免出现多条系统消息
            for i, msg in enumerate(messages):
                if isinstance(msg, SystemMessage):
                    merged = list(messages)
                    merged[i] = SystemMessage(
                        content=(content_text(msg.content) + reminder).strip()
                    )
                    return override(messages=merged)
            return override(
                messages=[SystemMessage(content=reminder.strip())] + messages
            )
        text = content_text(getattr(system_message, "content", ""))
        return override(system_message=SystemMessage(content=(text + reminder).strip()))
