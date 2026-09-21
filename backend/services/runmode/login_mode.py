"""login 模式: 自动运行 Mongo HEAD 代码至「登录+凭据保存」后结束(见规划第 6 章)。

- 复用既有 /run 独立登录协作(RunLoginManager / StandaloneLoginGate + page_login);
- 复用既有 CrawlerEnv 的 page_login / set_login_ticket / capture_login_state, 不新增登录组件;
- 结束判定: 编排层包装 set_login_ticket, 保存成功即抛 LoginComplete 结束运行;
- 对外仅暴露实时画面(BrowserStream + stream 路由), 由 CDC 内嵌观察。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from ..agent.bridge import BrowserBridge
from ..agent.run_login import RunLoginManager
from ..agent.session.event import EventHub
from .code_source import NoCodeError, load_head_code
from .run_context import LoginComplete


class LoginRunManager:
    """管理一次 login 运行的生命周期与状态(挂在 app.state.login_run)。"""

    def __init__(self) -> None:
        self.cfg: Any = None
        self.stream: Any = None
        self.run_login: RunLoginManager | None = None
        self.run_id = ""
        self.status = "pending"      # pending / running / succeeded / failed
        self.error = ""
        self.started_at = 0
        self.finished_at = 0
        self.result: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None

    def setup(self, cfg: Any, stream: Any, hub: EventHub | None = None) -> None:
        self.cfg = cfg
        self.stream = stream
        self.run_login = RunLoginManager(hub or EventHub())

    async def start(self) -> None:
        """启动 login 运行(后台任务, 复用 /run 登录链路)。"""
        self.run_id = uuid.uuid4().hex[:12]
        self.status = "running"
        self.started_at = int(time.time() * 1000)
        self._task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        try:
            await self._execute()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._finish("failed", f"{type(exc).__name__}: {exc}")

    async def _execute(self) -> None:
        if self.stream is None or self.run_login is None:
            self._finish("failed", "login 运行未初始化")
            return
        try:
            code = await load_head_code(self.cfg)
        except NoCodeError as exc:
            self._finish("failed", str(exc))
            return

        gate = self.run_login.new_gate(self.run_id, BrowserBridge(self.stream))
        gate._main_loop = asyncio.get_running_loop()

        async def on_credential_saved(ticket: Any, host: str) -> None:
            raise LoginComplete(f"凭据已保存 host={host}")

        timeout = max(1.0, float(getattr(self.cfg, "login_timeout", 300) or 300))
        try:
            result = await asyncio.wait_for(
                self.stream.run_code(
                    code,
                    login_gate=gate,
                    restart=True,
                    on_credential_saved=on_credential_saved,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self._finish("failed", f"登录超时({timeout}s)")
            return
        finally:
            self.run_login.remove(self.run_id)

        ok = bool((result or {}).get("ok"))
        if ok and (result or {}).get("login_complete"):
            self._finish("succeeded", "")
        elif ok:
            self._finish("failed", "脚本结束但未检测到凭据保存(login_not_saved)")
        else:
            self._finish("failed", (result or {}).get("error") or "登录失败")

    def _finish(self, status: str, error: str) -> None:
        self.status = status
        self.error = error
        self.finished_at = int(time.time() * 1000)

    def status_info(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "crawler_id": (self.cfg.crawler_id or "default") if self.cfg else "default",
        }