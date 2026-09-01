"""远程控制输入服务: 把前端实时画面上的交互事件注入浏览器页面。

    前端实时画面(图像像素坐标, 即 Xvfb 屏幕坐标) → CDP Input 域事件。
    坐标映射: 视口偏移 = 屏幕尺寸 - 页面视口尺寸(Page.getLayoutMetrics),
    视口坐标 = 屏幕坐标 - 偏移, 并夹紧在视口范围内(DPR=1, CSS 像素即设备像素)。

    协议(JSON, 见 routers/input.py /ws/input):
      mouse: {type:"mouse", action:"move"|"down"|"up"|"wheel",
              x, y, button?, buttons?, clickCount?, deltaX?, deltaY?, modifiers?}
      key:   {type:"key", action:"down"|"up"|"char",
              key, code, keyCode?, text?, modifiers?}
      touch: {type:"touch", action:"start"|"move"|"end", x, y, id?}
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

_BUTTON_NAMES = {0: "left", 1: "middle", 2: "right", 3: "back", 4: "forward"}
_METRICS_INTERVAL = 5.0  # 秒: 视口指标刷新最小间隔
_MOUSE_ACTIONS = ("move", "down", "up", "wheel")
_KEY_ACTIONS = ("down", "up", "char")
_TOUCH_ACTIONS = ("start", "move", "end")
_TEXT_ACTIONS = ("insert", "compose", "commit")
_MAX_TEXT_LEN = 8 * 1024  # 单条文本输入上限(字符)

# 判断当前聚焦元素是否可编辑(跨 iframe 只检查顶层 frame, 见 docs/text-input-plan.md)
_EDITABLE_JS = """(() => {
  const el = document.activeElement;
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = String(el.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea';
})()"""


def _modifiers(mods: Any) -> int:
    """{alt,ctrl,meta,shift} → CDP 修饰键位掩码 (Alt=1 Ctrl=2 Meta=4 Shift=8)。"""
    mods = mods if isinstance(mods, dict) else {}
    value = 0
    if mods.get("alt"):
        value |= 1
    if mods.get("ctrl"):
        value |= 2
    if mods.get("meta"):
        value |= 4
    if mods.get("shift"):
        value |= 8
    return value


def _int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc


def _coords(msg: dict) -> tuple[int, int]:
    return _int(msg.get("x"), "x"), _int(msg.get("y"), "y")


class InputInjector:
    """注入 CDP Input 事件, 负责坐标映射与鼠标移动合流。"""

    def __init__(self, stream):
        self.stream = stream
        self.cfg = stream.cfg
        self._viewport = (float(stream.cfg.width), float(stream.cfg.height))
        self._offset = (0.0, 0.0)
        self._metrics_at = 0.0
        self._lock = asyncio.Lock()
        self._pending_move: tuple[int, int, int, int] | None = None
        self._flushing = False
        self._ime_supported = True  # Input.imeSetComposition 等命令可用性(P3 组合预览)

    # ------------------------------------------------------------ 指标/坐标

    def viewport(self) -> dict:
        w, h = self._viewport
        return {"width": int(w), "height": int(h)}

    def offset(self) -> dict:
        x, y = self._offset
        return {"x": int(x), "y": int(y)}

    async def refresh_metrics(self, force: bool = False) -> bool:
        """查询页面视口尺寸并计算浏览器 UI 偏移(限频)。

        页面未加载/无会话/查询失败时保持上次指标, 返回 False。
        """
        now = time.monotonic()
        if not force and self._metrics_at and now - self._metrics_at < _METRICS_INTERVAL:
            return True
        session = self.stream.cdp.primary()
        if session is None:
            return False
        try:
            resp = await session.command("Page.getLayoutMetrics", {}, timeout=3.0)
        except Exception:  # noqa: BLE001
            return False
        if "error" in resp:
            return False
        vv = (resp.get("result") or {}).get("cssVisualViewport") or {}
        vw = vv.get("clientWidth")
        vh = vv.get("clientHeight")
        if vw and vh:
            self._viewport = (float(vw), float(vh))
            self._offset = (
                max(0.0, float(self.cfg.width) - float(vw)),
                max(0.0, float(self.cfg.height) - float(vh)),
            )
        self._metrics_at = now
        return True

    def _to_viewport(self, x: float, y: float) -> tuple[int, int]:
        ox, oy = self._offset
        vw, vh = self._viewport
        vx = x - ox
        vy = y - oy
        if vw > 0:
            vx = min(max(vx, 0.0), vw - 1.0)
        if vh > 0:
            vy = min(max(vy, 0.0), vh - 1.0)
        return int(round(vx)), int(round(vy))

    # ------------------------------------------------------------ 入口

    async def dispatch(self, msg: dict) -> dict:
        mtype = msg.get("type")
        if mtype == "mouse":
            return await self._mouse(msg)
        if mtype == "key":
            return await self._key(msg)
        if mtype == "touch":
            return await self._touch(msg)
        if mtype == "text":
            return await self._text(msg)
        return {"ok": False, "error": f"未知输入类型: {mtype}"}

    def _session(self) -> Any | None:
        return self.stream.cdp.primary()

    # ------------------------------------------------------------ 鼠标

    async def _mouse(self, msg: dict) -> dict:
        action = msg.get("action")
        if action not in _MOUSE_ACTIONS:
            return {"ok": False, "error": f"未知鼠标动作: {action}"}
        try:
            x, y = _coords(msg)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        modifiers = _modifiers(msg.get("modifiers"))
        buttons = _int(msg.get("buttons") or 0, "buttons")
        if action == "move":
            return await self.mouse_move(x, y, buttons, modifiers)
        await self.refresh_metrics()
        session = self._session()
        if session is None:
            return {"ok": False, "disabled": True, "error": "无活动页面"}
        vx, vy = self._to_viewport(x, y)
        if action == "wheel":
            try:
                delta_x = float(msg.get("deltaX") or 0)
                delta_y = float(msg.get("deltaY") or 0)
            except (TypeError, ValueError) as exc:
                return {"ok": False, "error": f"无效滚轮增量: {exc}"}
            await session.command("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": vx, "y": vy,
                "deltaX": delta_x, "deltaY": delta_y, "modifiers": modifiers,
            })
            return {"ok": True}
        button = _BUTTON_NAMES.get(_int(msg.get("button") or 0, "button"), "left")
        click_count = max(1, _int(msg.get("clickCount") or 1, "clickCount"))
        etype = "mousePressed" if action == "down" else "mouseReleased"
        await session.command("Input.dispatchMouseEvent", {
            "type": etype, "x": vx, "y": vy,
            "button": button, "buttons": buttons,
            "clickCount": click_count, "modifiers": modifiers,
        })
        return {"ok": True}

    async def mouse_move(self, x: int, y: int, buttons: int = 0,
                         modifiers: int = 0) -> dict:
        """鼠标移动: 合流高频 move, 在途时不并发注入只记最新位置。"""
        async with self._lock:
            self._pending_move = (x, y, buttons, modifiers)
            if self._flushing:
                return {"ok": True, "queued": True}
            self._flushing = True
        try:
            result: dict = {"ok": True}
            while True:
                async with self._lock:
                    pending = self._pending_move
                    self._pending_move = None
                if pending is None:
                    return result
                px, py, pb, pm = pending
                result = await self._dispatch_move(px, py, pb, pm)
        finally:
            async with self._lock:
                self._flushing = False

    async def _dispatch_move(self, x: int, y: int, buttons: int, modifiers: int) -> dict:
        await self.refresh_metrics()
        session = self._session()
        if session is None:
            return {"ok": False, "disabled": True, "error": "无活动页面"}
        vx, vy = self._to_viewport(x, y)
        await session.command("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": vx, "y": vy,
            "button": "none", "buttons": buttons, "modifiers": modifiers,
        })
        return {"ok": True}

    # ------------------------------------------------------------ 键盘

    async def _key(self, msg: dict) -> dict:
        action = msg.get("action")
        if action not in _KEY_ACTIONS:
            return {"ok": False, "error": f"未知键盘动作: {action}"}
        key = msg.get("key") or ""
        code = msg.get("code") or ""
        modifiers = _modifiers(msg.get("modifiers"))
        try:
            key_code = _int(msg.get("keyCode"), "keyCode") if msg.get("keyCode") is not None else None
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        text = msg.get("text")
        if text is not None and not isinstance(text, str):
            return {"ok": False, "error": "text 必须是字符串"}
        await self.refresh_metrics()
        session = self._session()
        if session is None:
            return {"ok": False, "disabled": True, "error": "无活动页面"}
        if action == "char":
            if not text:
                return {"ok": False, "error": "char 动作缺少 text"}
            await session.command("Input.dispatchKeyEvent", {
                "type": "char", "text": text, "key": key or text,
                "code": code, "modifiers": modifiers,
            })
            return {"ok": True}
        params: dict[str, Any] = {
            "type": "keyDown" if action == "down" else "keyUp",
            "key": key, "code": code, "modifiers": modifiers,
        }
        if key_code is not None:
            params["windowsVirtualKeyCode"] = key_code
            params["nativeVirtualKeyCode"] = key_code
        if action == "down" and text:
            params["text"] = text
        await session.command("Input.dispatchKeyEvent", params)
        return {"ok": True}

    # ------------------------------------------------------------ 触摸

    async def _touch(self, msg: dict) -> dict:
        action = msg.get("action")
        if action not in _TOUCH_ACTIONS:
            return {"ok": False, "error": f"未知触摸动作: {action}"}
        try:
            x, y = _coords(msg)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        touch_id = _int(msg.get("id") or 0, "id")
        await self.refresh_metrics()
        session = self._session()
        if session is None:
            return {"ok": False, "disabled": True, "error": "无活动页面"}
        vx, vy = self._to_viewport(x, y)
        etype = {"start": "touchStart", "move": "touchMove", "end": "touchEnd"}[action]
        points = (
            [{"x": vx, "y": vy, "id": touch_id, "radiusX": 1, "radiusY": 1, "force": 1}]
            if etype != "touchEnd" else []
        )
        await session.command("Input.dispatchTouchEvent", {
            "type": etype, "touchPoints": points,
        })
        return {"ok": True}

    # ------------------------------------------------------------ 文本

    async def _text(self, msg: dict) -> dict:
        action = msg.get("action") or "insert"
        if action not in _TEXT_ACTIONS:
            return {"ok": False, "error": f"未知文本动作: {action}"}
        text = msg.get("text")
        if not isinstance(text, str):
            return {"ok": False, "error": "text 必须是字符串"}
        if len(text) > _MAX_TEXT_LEN:
            return {"ok": False, "error": f"文本过长(上限 {_MAX_TEXT_LEN} 字符)"}
        if not text:
            return {"ok": True, "noop": True}
        await self.refresh_metrics()
        session = self._session()
        if session is None:
            return {"ok": False, "disabled": True, "error": "无活动页面"}
        if action == "insert":
            return await self._insert_text(session, text)
        if action == "compose":
            return await self._ime_compose(session, msg, text)
        return await self._ime_commit(session, msg, text)

    async def _insert_text(self, session: Any, text: str) -> dict:
        editable = await self._check_editable(session)
        if editable is False:
            return {"ok": False, "error": "页面光标不在可编辑元素上，请先在页面中点击一个输入框"}
        await session.command("Input.insertText", {"text": text})
        return {"ok": True}

    async def _check_editable(self, session: Any) -> bool | None:
        """判断聚焦元素是否可编辑; 查询失败返回 None(不阻塞插入)。"""
        try:
            resp = await session.command("Runtime.evaluate", {
                "expression": _EDITABLE_JS,
                "returnByValue": True,
            }, timeout=3.0)
        except Exception:  # noqa: BLE001
            return None
        if "error" in resp:
            return None
        result = resp.get("result") or {}
        if result.get("exceptionDetails"):
            return None
        return (result.get("result") or {}).get("value")

    async def _ime_compose(self, session: Any, msg: dict, text: str) -> dict:
        """组合预览: Input.imeSetComposition(Chrome 109+/118+)。不支持时忽略。"""
        if not self._ime_supported:
            return {"ok": True}
        params: dict[str, Any] = {"text": text}
        sel_start = msg.get("selectionStart")
        sel_end = msg.get("selectionEnd")
        if sel_start is not None:
            try:
                start = _int(sel_start, "selectionStart")
                end = _int(sel_end if sel_end is not None else sel_start, "selectionEnd")
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            params["selectionStart"] = start
            params["selectionEnd"] = end
        try:
            resp = await session.command("Input.imeSetComposition", params)
        except Exception:  # noqa: BLE001
            self._ime_supported = False
            return {"ok": True}
        if "error" in resp:
            self._ime_supported = False
        return {"ok": True}

    async def _ime_commit(self, session: Any, msg: dict, text: str) -> dict:
        """提交组合文本; 不支持 imeCommitComposition 时回退为 insertText。"""
        if self._ime_supported:
            try:
                resp = await session.command("Input.imeCommitComposition", {"text": text})
            except Exception:  # noqa: BLE001
                self._ime_supported = False
                resp = {"error": "unsupported"}
            if "error" not in resp:
                return {"ok": True}
            self._ime_supported = False
        return await self._insert_text(session, text)
