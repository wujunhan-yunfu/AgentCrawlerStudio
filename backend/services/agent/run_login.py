"""独立运行的登录协作: 让前端直接运行代码(编辑器执行 /run)时 page_login 也能与用户交互。

与 Agent 会话版 LoginGate 的区别: 不依赖 AgentSession, 直接绑定全局 EventHub,
事件按 run_id 标识; 前端在执行期间轮询 /run/{run_id}/login 获取登录请求,
并通过 /run/{run_id}/login-answer、/run/{run_id}/login-action 提交答案/触发动作。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .bridge import BrowserBridge
from .login import LoginDetector, _QR_TAB_JS, _data_uri


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
        # 独立运行时 run_code 在 worker 事件循环执行, 而 /run/*/login-answer 等
        # HTTP 处理器在主事件循环: 记录主循环用于跨线程安全投递。
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # 可选本地事件队列: /run SSE 流 attach 后, 登录事件同时推送到该队列,
        # 前端无需轮询即可实时收到登录请求/超时/成功(替代 /run/{id}/login 轮询)。
        self._local_q: asyncio.Queue | None = None

    # ---------------------------------------------------------- 交互主流程

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存登录请求并挂起等待前端答复。"""
        self._payload = dict(payload)
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self._emit(
            {
                "type": "run_login_request",
                "run_id": self.run_id,
                **payload,
            }
        )
        monitor = None
        if payload.get("login_type") == "qr":
            # QR 监听需要主事件循环里的 CDP 会话, 调度回主循环执行
            if self._main_loop is not None and loop is not self._main_loop:
                self._main_loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self._monitor_qr(payload))
                )
            else:
                monitor = loop.create_task(self._monitor_qr(payload))
        try:
            return await self._future
        finally:
            if monitor is not None and not monitor.done():
                monitor.cancel()
            self._future = None
            self._payload = None

    def _emit(self, event: dict[str, Any]) -> None:
        """本地 SSE 队列与事件总线都在主事件循环侧, 跨线程时统一安全投递。"""
        if self._main_loop is not None:
            try:
                cur = asyncio.get_running_loop()
            except RuntimeError:
                cur = None
            if cur is not self._main_loop:
                self._main_loop.call_soon_threadsafe(self._deliver, event)
                return
        self._deliver(event)

    def _deliver(self, event: dict[str, Any]) -> None:
        if self._local_q is not None:
            try:
                self._local_q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        self.hub.emit(event)

    def _resolve_future(self, value: dict[str, Any]) -> None:
        """跨线程安全地完成挂起脚本等待的 future。"""
        fut = self._future
        if fut is None or fut.done():
            return
        try:
            f_loop = fut.get_loop()
        except RuntimeError:
            fut.set_result(value)
            return
        try:
            cur = asyncio.get_running_loop()
        except RuntimeError:
            cur = None
        if cur is None or cur is f_loop:
            fut.set_result(value)
        else:
            f_loop.call_soon_threadsafe(fut.set_result, value)

    def answer(self, answers: dict[str, Any]) -> None:
        """前端提交登录答案, 恢复被 page_login 挂起的脚本。"""
        if self._future is None or self._future.done():
            raise ValueError("当前没有等待中的登录")
        if not self._payload:
            raise ValueError("登录请求已失效")
        self._resolve_future(answers)

    def payload(self) -> dict[str, Any] | None:
        return self._payload

    def attach(self, q: asyncio.Queue) -> None:
        """绑定本地事件队列(/run SSE 流), 登录事件同时推送到该队列。"""
        self._local_q = q

    def cancel(self) -> None:
        self._resolve_future({"cancelled": True})

    # ---------------------------------------------------------- 登录动作

    async def send_code(self) -> dict[str, Any]:
        captcha = (self._payload or {}).get("captcha") or {}
        sel = captcha.get("send_selector") or ""
        if not sel:
            return {"ok": False, "message": "未找到发送验证码按钮的选择器"}
        r = await self.bridge.evaluate(f"document.querySelector({sel!r})?.click(); true")
        ok = bool(isinstance(r, dict) and r.get("ok"))
        msg = "已触发发送验证码，请查看手机" if ok else f"触发失败: {r.get('error') if isinstance(r, dict) else '未知错误'}"
        self._emit({"type": "run_login_action", "run_id": self.run_id, "action": "send_code", "ok": ok, "message": msg})
        return {"ok": ok, "message": msg}

    async def refresh_captcha(self) -> dict[str, Any]:
        captcha = (self._payload or {}).get("captcha") or {}
        sel = captcha.get("refresh_selector") or captcha.get("image_selector") or ""
        if sel:
            await self.bridge.evaluate(f"document.querySelector({sel!r})?.click(); true")
            await asyncio.sleep(0.6)
        image = await self._captcha_image()
        msg = "验证码已刷新" if image else "未找到图形验证码元素"
        self._emit({"type": "run_login_action", "run_id": self.run_id, "action": "refresh_captcha", "ok": bool(image), "message": msg})
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

    async def refresh_qr(self) -> dict[str, Any]:
        """刷新二维码: 重新加载当前登录页以生成新二维码(供 /run/*/login-action 调用)。"""
        r = await self.bridge.evaluate("location.reload(); true")
        ok = bool(isinstance(r, dict) and r.get("ok"))
        await asyncio.sleep(0.8)
        await self.bridge.evaluate(_QR_TAB_JS)
        msg = "二维码已刷新，请用最新二维码重新扫码" if ok else "二维码刷新失败"
        self._emit({"type": "run_login_action", "run_id": self.run_id, "action": "refresh_qr", "ok": ok, "message": msg})
        return {"ok": ok, "message": msg}

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
                self._emit({"type": "run_login_timeout", "run_id": self.run_id})
                return
            r = await self.bridge.evaluate("location.href")
            cur = ""
            if isinstance(r, dict) and r.get("ok"):
                cur = (r.get("item") or {}).get("v") or ""
            if cur and LoginDetector.navigated_away(start, cur):
                self._resolve_future({"ok": True, "url": cur})
                return

    async def finish(self, method: str, url: str) -> None:
        self._emit({"type": "run_login_success", "run_id": self.run_id, "method": method, "url": url})