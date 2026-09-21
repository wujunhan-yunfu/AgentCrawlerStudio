"""数据 webhook 收集器: 每次 save_content 产生一条 data.saved 推送(见规划 7.12)。

- 未配置数据 webhook 地址时禁用(仅本地落盘, 行为不变);
- 默认异步: 有界队列 + 后台发送, 失败重试; 运行结束事件前调用 flush();
- 同步模式(--data-webhook-sync): save_content 内联等待 CDC 确认;
- 大文件(> data_inline_max_bytes)预留预签名直传, 本期统一内联推送。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import Any

from .run_context import now_ms
from .webhook import WebhookSender

_EVENT = "data.saved"
_FMT_CONTENT_TYPE = {
    "txt": "text/plain",
    "json": "application/json",
    "jsonl": "application/x-ndjson",
    "csv": "text/csv",
    "html": "text/html",
    "img": "image/png",
}


class DataCollector:
    """按 run 收集并投递 save_content 产生的数据条目。"""

    def __init__(
        self,
        cfg: Any,
        run_context: Any,
        sender: WebhookSender,
        sync: bool = False,
        inline_max_bytes: int = 1024 * 1024,
    ) -> None:
        self.cfg = cfg
        self.ctx = run_context
        self.sender = sender
        self.sync = bool(sync)
        self.inline_max_bytes = inline_max_bytes
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._delivered: dict[str, str] = {}  # save_id -> delivered / failed
        self._errors: dict[str, str] = {}     # save_id -> error 摘要

    @property
    def enabled(self) -> bool:
        return self.sender.enabled

    def save_id_for(self, item: dict[str, Any]) -> str:
        """稳定 save_id: 同 run 内同名同尺寸数据幂等。"""
        raw = f"{self.ctx.crawler_id}:{self.ctx.run_id}:{item.get('name')}:{item.get('size')}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def delivery_for(self, save_id: str) -> str:
        return self._delivered.get(save_id, "pending")

    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._queue = asyncio.Queue()
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._send_one(item)
            finally:
                self._queue.task_done()

    async def collect(self, item: dict[str, Any]) -> None:
        """save_content 保存成功后调用: 入队或同步发送一条 data.saved。"""
        if not self.enabled:
            return
        payload = self._build_payload(item)
        if self.sync:
            await self._send_one(payload)
        else:
            if self._queue is None:
                self.start()
            self._queue.put_nowait(payload)

    def _build_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        fmt = (item.get("fmt") or "txt")
        kind = item.get("kind") or "content"
        content = item.get("content", "")
        encoding = "base64" if fmt == "img" else "utf-8"
        save_id = self.save_id_for(item)
        seq = self.ctx.bump_seq()
        return {
            "event": _EVENT,
            "delivery_id": uuid.uuid4().hex,
            "timestamp": now_ms(),
            "data": {
                "crawler_id": self.ctx.crawler_id,
                "project_id": self.ctx.project_id,
                "workspace_id": self.ctx.workspace_id,
                "run_id": self.ctx.run_id,
                "save_id": save_id,
                "seq": seq,
                "kind": kind,
                "fmt": fmt,
                "name": item.get("name", ""),
                "size": item.get("size", 0),
                "content_hash": f"sha256:{self._content_hash(content)}",
                "content_type": _FMT_CONTENT_TYPE.get(fmt, "application/octet-stream"),
                "encoding": encoding,
                "content": content,
                "storage": {"mode": "inline"},
                "source_url": "",
                "collected_at": now_ms(),
            },
        }

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    async def _send_one(self, payload: dict[str, Any]) -> None:
        data = payload.get("data") or {}
        save_id = data.get("save_id", "")
        try:
            await self.sender.send_with_retry(_EVENT, payload)
            self._delivered[save_id] = "delivered"
        except Exception as exc:  # noqa: BLE001
            self._delivered[save_id] = "failed"
            self._errors[save_id] = str(exc)[:300]

    async def flush(self) -> None:
        """等待队列全部投递完毕(异步模式下运行结束事件前调用)。"""
        if self._queue is not None:
            await self._queue.join()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            self._queue = None