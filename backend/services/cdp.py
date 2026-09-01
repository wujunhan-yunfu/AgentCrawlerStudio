"""通用 CDP 会话管理: 多 target 会话、命令/响应关联、频道扇出、事件分发。

    Chrome(--remote-debugging-port)
        |  GET /json → 每个 page target 的 webSocketDebuggerUrl
        v
    CDPManager (轮询目标 + 每 target 一条 CDPSession)
        |  基础域(Runtime/Log/Page) + 各频道注册的域 enable
        |  事件按域分发到 Console/Network/DOM/Storage 频道
        |  命令/响应关联 → evaluate / getProperties / 各频道命令
        v
    频道(Channel)扇出 → /ws/console /ws/network /ws/dom /ws/storage
"""

from __future__ import annotations

import asyncio
import collections
import json
from typing import Any, Awaitable, Callable

import httpx
import websockets

_MAX_SUB_QUEUE = 1000
_MAX_HISTORY = 500
_SCAN_INTERVAL = 1.0
# 单条 CDP 消息上限: 某些页面(如百度安全验证)会输出 >8MB 的 console 消息
# (巨大 stackTrace), 过小会触发 WebSocket 1009 断开, 导致会话消失/控制失效。
_MAX_CDP_FRAME = 256 * 1024 * 1024
_STACK_MAX_FRAMES = 100
_STACK_MAX_CHARS = 16 * 1024

EventHandler = Callable[[Any, str, dict[str, Any]], Awaitable[bool]]


class Subscriber:
    """单客户端消息队列: 有界 deque + asyncio 唤醒事件"""

    def __init__(self, maxlen: int = _MAX_SUB_QUEUE):
        self._queue: collections.deque[str] = collections.deque(maxlen=maxlen)
        self._event = asyncio.Event()

    def push(self, item: str) -> None:
        self._queue.append(item)
        self._event.set()

    async def wait(self, timeout: float | None = None) -> str | None:
        loop = asyncio.get_running_loop()
        deadline = (loop.time() + timeout) if timeout is not None else None
        while not self._queue:
            if deadline is None:
                wait_for = None
            else:
                wait_for = deadline - loop.time()
                if wait_for <= 0:
                    return None
            try:
                await asyncio.wait_for(self._event.wait(), wait_for)
            except asyncio.TimeoutError:
                return None
            self._event.clear()
        return self._queue.popleft()


class Channel:
    """命名频道: 消息扇出 + 最近历史回放(供 /ws/* 订阅)"""

    def __init__(self, name: str, max_history: int = _MAX_HISTORY):
        self.name = name
        self._subs: list[Subscriber] = []
        self._history: collections.deque[str] = collections.deque(maxlen=max_history)
        self._lock = asyncio.Lock()

    def reset(self) -> None:
        self._subs.clear()
        self._history.clear()

    def clear_history(self) -> None:
        """清空历史回放(如网络记录被清空时), 新订阅者不再收到旧消息"""
        self._history.clear()

    async def attach(self) -> Subscriber:
        sub = Subscriber()
        async with self._lock:
            self._subs.append(sub)
            history = list(self._history)
        for item in history:
            sub.push(item)
        return sub

    async def detach(self, sub: Subscriber) -> None:
        async with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    async def publish(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False)
        async with self._lock:
            subs = list(self._subs)
            self._history.append(payload)
        for sub in subs:
            sub.push(payload)

    def count(self) -> int:
        return len(self._subs)


class CDPSession:
    """一条到 page target 的 CDP 会话: 事件监听 + 命令/响应关联。"""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws: Any | None = None
        self.ready = asyncio.Event()
        self.queue: asyncio.Queue[Any] | None = None
        self.group_depth = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 100

    async def command(self, method: str, params: dict[str, Any] | None = None,
                      timeout: float = 5.0) -> dict[str, Any]:
        ws = self.ws
        if ws is None:
            raise RuntimeError("CDP 会话未连接")
        loop = asyncio.get_running_loop()
        mid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = loop.create_future()
        self._pending[mid] = fut
        payload: dict[str, Any] = {"id": mid, "method": method}
        if params:
            payload["params"] = params
        await ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    def resolve(self, message: dict[str, Any]) -> None:
        mid = message.get("id")
        if not isinstance(mid, int):
            return
        fut = self._pending.get(mid)
        if fut is not None and not fut.done():
            fut.set_result(message)


class CDPManager:
    """管理到所有 page target 的 CDP 会话, 并把事件分发给各频道处理器。

    浏览器随时重启/换 CDP 端口(每次执行代码前重启 Chrome),
    采用"周期性扫描 /json + 会话自愈": 旧会话断开即清理, 新目标即连接。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.loop: asyncio.AbstractEventLoop | None = None
        self._channels: dict[str, Channel] = {}
        self._handlers: list[Any] = []
        self._target_urls: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._task_url: dict[asyncio.Task, str] = {}
        self._connected: set[str] = set()
        self._sessions: dict[str, CDPSession] = {}
        self._oid_session: dict[str, str] = {}
        self._rescan_evt = asyncio.Event()
        self._stop = False
        self._client: httpx.AsyncClient | None = None
        self.console: Any = None
        self.network: Any = None
        self.dom: Any = None
        self.storage: Any = None

    # ------------------------------------------------------------ 注册

    def register_channel(self, handler: Any, max_history: int = _MAX_HISTORY) -> Channel:
        name = handler.name
        channel = Channel(name, max_history)
        self._channels[name] = channel
        self._handlers.append(handler)
        setattr(self, name, handler)
        return channel

    def channel(self, name: str) -> Channel:
        return self._channels[name]

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stop = False
        self._client = httpx.AsyncClient(timeout=1.0)
        asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop = True
        tasks = list(self._tasks)
        self._tasks.clear()
        self._task_url.clear()
        self._target_urls.clear()
        self._sessions.clear()
        for task in tasks:
            task.cancel()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        for channel in self._channels.values():
            channel.reset()

    def rescan(self) -> None:
        """请求立即重新扫描 CDP 目标(浏览器重启后调用)"""
        if self.loop is not None and not self._stop:
            self._rescan_evt.set()

    def status(self) -> dict:
        return {
            "targets": len(self._target_urls),
            "connections": len(self._tasks),
            "history": len(self._channels.get("console", Channel("c"))._history),
            "subscribers": {name: ch.count() for name, ch in self._channels.items()},
        }

    # ------------------------------------------------------------ 扫描/会话

    async def _poll_loop(self) -> None:
        while not self._stop:
            await self._scan()
            try:
                await asyncio.wait_for(self._rescan_evt.wait(), timeout=_SCAN_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self._rescan_evt.clear()

    async def _scan(self) -> None:
        if self.cfg.cdp_port <= 0 or self._client is None:
            return
        try:
            resp = await self._client.get(f"http://127.0.0.1:{self.cfg.cdp_port}/json")
            resp.raise_for_status()
            targets: list[dict] = resp.json()
        except Exception:  # noqa: BLE001  浏览器未就绪/已停止
            return
        for t in targets:
            if t.get("type") != "page":
                continue
            if (t.get("url") or "").startswith("devtools://"):
                continue
            ws_url = t.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            if ws_url in self._target_urls:
                continue
            self._target_urls.add(ws_url)
            task = asyncio.create_task(self._listen_target(ws_url))
            task.add_done_callback(self._cleanup_task)
            self._task_url[task] = ws_url
            self._tasks.append(task)

    async def connect_now(self) -> None:
        """立即扫描并等待所有目标监听就绪(浏览器重启后调用, 消除首屏竞态)。"""
        if self.loop is None or self._stop:
            return
        await self._scan()
        want = set(self._target_urls)
        for _ in range(50):  # 最多 5s
            if want <= self._connected:
                return
            await asyncio.sleep(0.1)
        self._rescan_evt.set()

    def _cleanup_task(self, task: asyncio.Task) -> None:
        ws_url = self._task_url.pop(task, None)
        if ws_url:
            self._target_urls.discard(ws_url)
            self._connected.discard(ws_url)
            self._sessions.pop(ws_url, None)
        if task in self._tasks:
            self._tasks.remove(task)

    def _enable_commands(self) -> list[tuple[str, dict[str, Any]]]:
        cmds: list[tuple[str, dict[str, Any]]] = [
            ("Runtime.enable", {}),
            ("Log.enable", {}),
            ("Page.enable", {}),
        ]
        seen = {m for m, _ in cmds}
        for handler in self._handlers:
            for method, params in getattr(handler, "domains", []):
                if method not in seen:
                    seen.add(method)
                    cmds.append((method, params))
        return cmds

    async def _listen_target(self, ws_url: str) -> None:
        session = CDPSession(ws_url)
        self._sessions[ws_url] = session
        session.queue = asyncio.Queue()
        consumer = asyncio.create_task(self._process_session_events(session))
        reader: asyncio.Task | None = None
        try:
            async with websockets.connect(ws_url, max_size=_MAX_CDP_FRAME) as ws:
                session.ws = ws
                reader = asyncio.create_task(self._read_session(session))
                try:
                    await asyncio.wait_for(asyncio.gather(*[
                        session.command(method, params, timeout=5)
                        for method, params in self._enable_commands()
                    ]), timeout=8)
                    self._connected.add(ws_url)
                    session.ready.set()
                except Exception:  # noqa: BLE001
                    pass
                await reader
        except Exception:  # noqa: BLE001  目标已销毁/浏览器重启, 自愈
            pass
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
            session.queue.put_nowait(None)

    async def _read_session(self, session: CDPSession) -> None:
        ws = session.ws
        if ws is None:
            return
        async for raw in ws:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if "id" in message:
                session.resolve(message)
            else:
                method = message.get("method")
                if method and session.queue is not None:
                    await session.queue.put((method, message.get("params") or {}))

    async def _process_session_events(self, session: CDPSession) -> None:
        queue = session.queue
        if queue is None:
            return
        while True:
            item = await queue.get()
            if item is None:
                return
            method, params = item
            try:
                await self.dispatch(session, method, params)
            except Exception:  # noqa: BLE001
                pass

    async def dispatch(self, session: CDPSession, method: str, params: dict[str, Any]) -> bool:
        for handler in self._handlers:
            try:
                if await handler.on_event(session, method, params):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    # ------------------------------------------------------------ 命令辅助

    def primary(self) -> CDPSession | None:
        """选取"当前活动页面"会话: 优先非 about:blank 目标, 其次最近连接。"""
        candidates = [s for s in self._sessions.values() if s.ready.is_set()]
        if not candidates:
            return None
        pages = [s for s in candidates if s.ws_url in self._target_urls] or candidates
        for s in reversed(pages):
            return s
        return None

    def session_for(self, ws_url: str | None) -> CDPSession | None:
        if ws_url and ws_url in self._sessions:
            sess = self._sessions[ws_url]
            if sess.ready.is_set():
                return sess
        return self.primary()

    # ------------------------------------------------------------ 求值/展开

    async def evaluate(self, expression: str, timeout: float = 5.0) -> dict[str, Any]:
        """在活动页面执行 JS, 返回结果 segment(支持展开对象)。"""
        session = self.primary()
        if session is None:
            return {"ok": False, "error": "浏览器控制台未连接(无活动页面)"}
        try:
            resp = await session.command("Runtime.evaluate", {
                "expression": expression,
                "awaitPromise": True,
                "generatePreview": True,
                "returnByValue": False,
                "replMode": True,
                "userGesture": True,
            }, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        result = resp.get("result") or {}
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            exception_obj = details.get("exception") or {}
            text = exception_obj.get("description") or exception_obj.get("value") or details.get("text") or "Error"
            return {
                "ok": False,
                "error": str(text).strip(),
                "stack": self._stack_text(details.get("stackTrace")),
            }
        item = self.remote_item(session, result.get("result") or {})
        return {"ok": True, "item": item}

    async def get_properties(self, object_id: str, timeout: float = 5.0) -> dict[str, Any]:
        """获取对象可展开属性(Runtime.getProperties)。"""
        ws_url = self._oid_session.get(object_id)
        session = self._sessions.get(ws_url) if ws_url else None
        if session is None or not session.ready.is_set():
            session = self.primary()
        if session is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await session.command("Runtime.getProperties", {
                "objectId": object_id,
                "ownProperties": True,
                "accessorPropertiesOnly": False,
                "generatePreview": True,
            }, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        props = []
        for p in (resp.get("result") or {}).get("result") or []:
            name = p.get("name")
            if name in ("__proto__", "__defineGetter__", "__defineSetter__"):
                continue
            value = p.get("value") or {"type": "undefined"}
            props.append({"name": name, "item": self.remote_item(session, value)})
        return {"ok": True, "props": props}

    # ------------------------------------------------------------ RemoteObject

    def remote_item(self, session: CDPSession | None, obj: dict[str, Any]) -> dict[str, Any]:
        otype = obj.get("type") or "undefined"
        if otype == "string":
            return {"k": "text", "t": "str", "v": obj.get("value") or ""}
        if otype == "number":
            return {"k": "text", "t": "num", "v": str(obj.get("value"))}
        if otype == "boolean":
            return {"k": "text", "t": "bool", "v": "true" if obj.get("value") else "false"}
        if otype == "undefined":
            return {"k": "text", "t": "undef", "v": "undefined"}
        if otype == "null":
            return {"k": "text", "t": "null", "v": "null"}
        if otype == "bigint":
            return {"k": "text", "t": "num", "v": str(obj.get("value", ""))}
        item: dict[str, Any] = {
            "k": "obj",
            "v": obj.get("description") or f"<{obj.get('subtype') or otype}>",
            "oid": obj.get("objectId") or None,
            "sub": obj.get("subtype") or None,
            "cls": obj.get("className") or None,
        }
        oid = obj.get("objectId")
        if oid and session is not None:
            self._oid_session[str(oid)] = session.ws_url
            item["ou"] = session.ws_url
        preview = obj.get("preview")
        if preview and preview.get("properties"):
            item["prev"] = [
                {"n": p.get("name"), "v": p.get("value"), "t": p.get("type")}
                for p in preview.get("properties", [])[:5]
            ]
        if item["sub"] == "error":
            item["v"] = obj.get("description") or "Error"
        return item

    @staticmethod
    def _stack_text(stack: dict[str, Any] | None) -> str | None:
        frames = (stack or {}).get("callFrames") or []
        if not frames:
            return None
        omitted = len(frames) - _STACK_MAX_FRAMES
        lines = []
        for frame in frames[:_STACK_MAX_FRAMES]:
            url = frame.get("url") or "<anonymous>"
            func = frame.get("functionName") or "<anonymous>"
            line_no = (frame.get("lineNumber") or 0) + 1
            col_no = (frame.get("columnNumber") or 0) + 1
            lines.append(f"  at {func} ({url}:{line_no}:{col_no})")
        if omitted > 0:
            lines.append(f"  ... 共省略 {omitted} 帧")
        text = "\n".join(lines)
        if len(text) > _STACK_MAX_CHARS:
            text = text[:_STACK_MAX_CHARS] + "\n  ... 堆栈过长, 已截断"
        return text

    @staticmethod
    def _remote_str(arg: dict[str, Any]) -> str:
        otype = arg.get("type")
        value = arg.get("value")
        if otype == "string":
            return str(value)
        if otype == "undefined":
            return "undefined"
        if otype == "null":
            return "null"
        if value is not None:
            return str(value)
        return arg.get("description") or f"<{arg.get('subtype') or otype}>"
