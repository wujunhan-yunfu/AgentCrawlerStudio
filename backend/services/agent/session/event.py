"""事件总线: 单会话/全局事件扇出 + 有界历史回放。

供 WebSocket 订阅实时事件, 新订阅者接入时自动回放历史事件。
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

_EVENT_BUFFER = 1000

# 临时性事件不写入历史缓冲(仅实时推送), 避免把规划/任务/问卷等重要事件挤出缓冲。
_EPHEMERAL_TYPES = {"delta", "ping"}


class EventHub:
    """事件扇出总线: 追加历史 + 推送给所有订阅者。"""

    def __init__(self, maxlen: int = _EVENT_BUFFER):
        self._buffer: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._subs: list[asyncio.Queue] = []

    def emit(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
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

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subs.append(q)
        for event in self._buffer:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                break
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)
