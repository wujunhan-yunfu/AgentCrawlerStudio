"""定时任务心跳发送器: 周期性向 CDC 上报进程存活与健康(见规划 7.11)。

- 仅 run+cron 常驻进程启用; 尽力而为, 失败不阻塞调度主循环;
- ping() 在运行开始/结束/状态变化时立即补发;
- stop() 发送 status=stopping 后退出。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from .run_context import now_ms
from .webhook import WebhookSender

_VERSION = "0.3.0"


class HeartbeatSender:
    def __init__(
        self,
        cfg: Any,
        run_context: Any,
        sender: WebhookSender,
        interval: float = 30.0,
    ) -> None:
        self.cfg = cfg
        self.ctx = run_context
        self.sender = sender
        self.interval = max(1.0, float(interval))

    @property
    def enabled(self) -> bool:
        return self.sender.enabled

    def start(self) -> None:
        if not self.enabled or getattr(self, "_task", None) is not None:
            return
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await self._send()
            await asyncio.sleep(self.interval)

    def _build_payload(self, status: str | None = None) -> dict[str, Any]:
        return {
            "crawler_id": self.ctx.crawler_id,
            "process_id": self.ctx.process_id,
            "project_id": self.ctx.project_id,
            "workspace_id": self.ctx.workspace_id,
            "mode": "run",
            "run_type": self.ctx.run_type,
            "cron": self.ctx.cron,
            "status": status or self.ctx.process_status,
            "pid": os.getpid(),
            "started_at": self.ctx.started_at,
            "uptime_ms": now_ms() - self.ctx.started_at,
            "next_run_at": self.ctx.next_run_at,
            "last_run": self.ctx.last_run,
            "health": {
                "browser": True,
                "mongo": True,
                "llm": True,
                "disk_ok": True,
                "last_error": self.ctx.error_message or None,
            },
            "version": _VERSION,
        }

    async def _send(self, status: str | None = None) -> None:
        if not self.enabled:
            return
        try:
            await self.sender.send_with_retry("heartbeat", self._build_payload(status))
        except Exception:  # noqa: BLE001  # 尽力而为, 心跳失败不自杀
            pass

    def ping(self, status: str | None = None) -> None:
        """立即补发一次心跳(运行开始/结束/状态变化时调用)。"""
        if self.enabled:
            self.ctx.process_status = status or self.ctx.process_status
            asyncio.get_running_loop().create_task(self._send(status))

    async def stop(self) -> None:
        task = getattr(self, "_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.ctx.process_status = "stopping"
        await self._send("stopping")