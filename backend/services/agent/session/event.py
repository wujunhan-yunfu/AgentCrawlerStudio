"""事件总线: 单会话/全局事件扇出 + 有界历史回放。

供 WebSocket 订阅实时事件, 新订阅者接入时自动回放历史事件。
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from typing import Any

_EVENT_BUFFER = 1000

# 临时性事件不写入历史缓冲(仅实时推送), 避免把规划/任务/问卷等重要事件挤出缓冲。
_EPHEMERAL_TYPES = {"delta", "ping"}

# 需要落库(写入 agent_messages)的事件类型: 供前端刷新/切换会话后重建完整执行记录。
# message_end 携带模型本轮输出文本(含调用工具前的过渡文本), 落库为 assistant 消息。
PERSISTED_EVENT_TYPES = {
    "status",
    "error",
    "plan",
    "todos",
    "saved",
    "question",
    "answer",
    "login_timeout",
    "tool",
    "tool_result",
    "editor_code",
    "login_request",
    "login_success",
    "login_action",
    "message_end",
}


class EventHub:
    """事件扇出总线: 追加历史 + 推送给所有订阅者。

    - 全局 _buffer 保留原有行为(兼容测试/无会话事件);
    - 每个会话另有独立缓冲与单调 seq, WS 可用 ?session=&since= 增量回放,
      避免重连时重复消费全局历史;
    - epoch 标识本次后端进程, 前端据此在后端重启后重置去重游标。
    """

    def __init__(self, maxlen: int = _EVENT_BUFFER):
        self._buffer: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._subs: list[asyncio.Queue] = []
        self._maxlen = maxlen
        self._session_buffers: dict[str, deque[dict[str, Any]]] = {}
        self._seq: dict[str, int] = {}
        self.epoch = uuid.uuid4().hex[:12]

    def latest_seq(self, session_id: str) -> int:
        return self._seq.get(session_id, 0)

    def drop_session(self, session_id: str) -> None:
        """会话删除/淘汰时释放其事件缓冲与序号。"""
        self._session_buffers.pop(session_id, None)
        self._seq.pop(session_id, None)

    def emit(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        sid = event.get("session_id")
        if sid:
            seq = self._seq.get(sid, 0) + 1
            self._seq[sid] = seq
            event["seq"] = seq
            if etype not in _EPHEMERAL_TYPES:
                buf = self._session_buffers.get(sid)
                if buf is None:
                    buf = deque(maxlen=self._maxlen)
                    self._session_buffers[sid] = buf
                buf.append(event)
        if etype not in _EPHEMERAL_TYPES:
            self._buffer.append(event)
        dead: list[asyncio.Queue] = []
        for q in self._subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 重要事件不可丢弃, 队列满则摘掉该订阅者; 临时事件丢弃即可
                if etype not in _EPHEMERAL_TYPES:
                    dead.append(q)
        for q in dead:
            self._subs.remove(q)

    def subscribe(
        self, session_id: str | None = None, since: int | None = None
    ) -> asyncio.Queue:
        """订阅事件; 指定 session_id 时只回放该会话 seq > since 的事件。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subs.append(q)
        if session_id:
            source = list(self._session_buffers.get(session_id, ()))
            floor = int(since or 0)
            source = [e for e in source if int(e.get("seq") or 0) > floor]
        else:
            source = list(self._buffer)
        for event in source:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                break
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)
