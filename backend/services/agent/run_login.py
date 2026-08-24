"""独立运行的登录协作: 让前端直接运行代码(编辑器执行 /run)时 page_login 也能与用户交互。

与 Agent 会话版 LoginGate 的区别: 不依赖 AgentSession, 直接绑定全局 EventHub,
事件按 run_id 标识; 前端在执行期间轮询 /run/{run_id}/login 获取登录请求,
并通过 /run/{run_id}/login-answer、/run/{run_id}/login-action 提交答案/触发动作。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .bridge import BrowserBridge
from .login import LoginDetector, _data_uri


class RunLoginManager:
    """按 run_id 管理独立运行的登录协作状态, 挂在 app.state.run_login。"""

    def __init__(self, hub: Any) -> None:
        self.hub = hub
        self._gates: dict[str, "StandaloneLoginGate"] = {}

    def new_gate(self, run_id: str, bridge: BrowserBridge) -> "StandaloneLoginGate":
        gate = StandaloneLoginGate(run_id, self.hub, bridge)
        self._gates[run_id] = gate
        return gate

    def get_gate(self, run_id: str) -> "StandaloneLoginGate | None":
        return self._gates.get(run_id)

    def remove(self, run_id: str) -> None:
        gate = self._gates.pop(run_id, None)
        if gate is not None:
            gate.cancel()


class StandaloneLoginGate:
    """供 page_login 挂起独立运行脚本, 与前端直接交互(非 Agent 会话)。"""

    def __init__(self, run_id: str, hub: Any, bridge: BrowserBridge) -> None:
        self.run_id = run_id
        self.hub = hub
        self.bridge = bridge
        self._payload: dict[str, Any] | None = None
        self._future: asyncio.Future | None = None

    # ---------------------------------------------------------- 交互主流程

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存登录请求并挂起等待前端答复。"""
        self._payload = dict(payload)
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self.hub.emit(
            {
                "type": "run_login_request",
                "run_id": self.run_id,
                **payload,
            }
        )
        monitor = None
        if payload.get("login_type") == "qr":
            monitor = loop.create_task(self._monitor_qr(payload))
        try:
            return await self._future
        finally:
            if monitor is not None and not monitor.done():
                monitor.cancel()
            self._future = None
            self._payload = None

    def answer(self, answers: dict[str, Any]) -> None:
        """前端提交登录答案, 恢复被 page_login 挂起的脚本。"""
        if self._future is None or self._future.done():
            raise ValueError("当前没有等待中的登录")
        if not self._payload:
            raise ValueError("登录请求已失效")
        self._future.set_result(answers)

    def payload(self) -> dict[str, Any] | None:
        return self._payload

    def cancel(self) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_result({"cancelled": True})

    # ---------------------------------------------------------- 登录动作

    async def send_code(self) -> dict[str, Any]:
        captcha = (self._payload or {}).get("captcha") or {}
        sel = captcha.get("send_selector") or ""
        if not sel:
            return {"ok": False, "message": "未找到发送验证码按钮的选择器"}
        r = await self.bridge.evaluate(f"document.querySelector({sel!r})?.click(); true")
        ok = bool(isinstance(r, dict) and r.get("ok"))
        msg = "已触发发送验证码，请查看手机" if ok else f"触发失败: {r.get('error') if isinstance(r, dict) else '未知错误'}"
        self.hub.emit({"type": "run_login_action", "run_id": self.run_id, "action": "send_code", "ok": ok, "message": msg})
        return {"ok": ok, "message": msg}

    async def refresh_captcha(self) -> dict[str, Any]:
        captcha = (self._payload or {}).get("captcha") or {}
        sel = captcha.get("refresh_selector") or captcha.get("image_selector") or ""
        if sel:
            await self.bridge.evaluate(f"document.querySelector({sel!r})?.click(); true")
            await asyncio.sleep(0.6)
        image = await self._captcha_image()
        msg = "验证码已刷新" if image else "未找到图形验证码元素"
        self.hub.emit({"type": "run_login_action", "run_id": self.run_id, "action": "refresh_captcha", "ok": bool(image), "message": msg})
        return {"ok": bool(image), "message": msg, "image": image}

    async def _captcha_image(self) -> str | None:
        captcha = (self._payload or {}).get("captcha") or {}
        sel = captcha.get("image_selector") or ""
        if not sel:
            return None
        try:
            data = await self.bridge.element_shot(sel)
            return _data_uri(data)
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------- QR 监听

    async def _monitor_qr(self, payload: dict[str, Any]) -> None:
        start = payload.get("url") or ""
        timeout = float(payload.get("timeout") or 180)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            await asyncio.sleep(1.5)
            fut = self._future
            if fut is None or fut.done():
                return
            if loop.time() > deadline:
                self.hub.emit({"type": "run_login_timeout", "run_id": self.run_id})
                return
            r = await self.bridge.evaluate("location.href")
            cur = ""
            if isinstance(r, dict) and r.get("ok"):
                cur = (r.get("item") or {}).get("v") or ""
            if cur and LoginDetector.navigated_away(start, cur):
                fut.set_result({"ok": True, "url": cur})
                return

    async def finish(self, method: str, url: str) -> None:
        self.hub.emit({"type": "run_login_success", "run_id": self.run_id, "method": method, "url": url})