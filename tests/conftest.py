"""共享测试夹具: 环境准备 / MongoDB 模拟 / 常用 fixture。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

# ---------------------------------------------------------------------------
# mongomock <-> pymongo 兼容补丁: pymongo>=4.14 的 UpdateOne/ReplaceOne/DeleteOne
# 在 _add_to_bulk 时传入 sort= 关键字, 老版本 mongomock 的 BulkOperationBuilder
# 不支持该参数。这里是测试专用的兼容垫片, 不改变被测代码行为。
# ---------------------------------------------------------------------------

_patched = False


def _patch_mongomock() -> None:
    global _patched
    if _patched:
        return
    try:
        from mongomock.collection import BulkOperationBuilder

        def _accept_sort(func):
            def wrapper(self, *args, **kwargs):
                kwargs.pop("sort", None)
                kwargs.pop("let", None)
                return func(self, *args, **kwargs)

            return wrapper

        BulkOperationBuilder.add_update = _accept_sort(BulkOperationBuilder.add_update)
        BulkOperationBuilder.add_replace = _accept_sort(BulkOperationBuilder.add_replace)
        BulkOperationBuilder.add_delete = _accept_sort(BulkOperationBuilder.add_delete)
    except ImportError:  # mongomock 未安装时跳过
        pass
    _patched = True


_patch_mongomock()


# ---------------------------------------------------------------------------
# 基础 fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_modules(monkeypatch: pytest.MonkeyPatch):
    """清理单例状态/模块缓存, 让每个用例独立。"""
    yield


@pytest.fixture()
def cfg() -> Any:
    from backend.config import Config

    return Config()


@pytest.fixture()
def event_loop_policy():
    """pytest-asyncio 使用独立事件循环, 无特殊配置。"""
    return None


@pytest.fixture()
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# 通用假对象
# ---------------------------------------------------------------------------


class FakePage:
    """极简 playwright Page 假对象, 供 login/crawler 等测试使用。"""

    def __init__(self, url: str = "https://example.com/", title: str = "title"):
        self._url = url
        self._title = title
        self.evaluations: list[str] = []
        self.fills: list[tuple[str, str]] = []

    @property
    def url(self) -> str:
        return self._url

    async def title(self) -> str:
        return self._title

    async def goto(self, url: str, **kwargs) -> None:
        self._url = url

    async def content(self) -> str:
        return "<html><body>hello</body></html>"

    async def evaluate(self, expr: str, *args, **kwargs):
        self.evaluations.append(expr)
        return None

    def locator(self, selector: str):
        return FakeLocator(self, selector)

    async def fill(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))

    async def click(self, selector: str, **kwargs) -> None:
        return None

    async def screenshot(self, **kwargs) -> bytes:
        return b"fake-png"

    async def add_init_script(self, script: str) -> None:
        return None


class FakeLocator:
    def __init__(self, page: FakePage, selector: str):
        self.page = page
        self.selector = selector
        self.first = self

    async def screenshot(self, **kwargs) -> bytes:
        return b"fake-element-png"

    async def click(self, **kwargs) -> None:
        return None


class FakeContext:
    """极简 BrowserContext 假对象。"""

    def __init__(self, pages: list | None = None):
        self._pages = pages if pages is not None else []
        self.cookies_added: list[list[dict]] = []
        self.cookies_requested: list[list[str]] = []
        self._cdp_session = None

    @property
    def pages(self) -> list:
        return self._pages

    async def new_page(self) -> FakePage:
        page = FakePage()
        self._pages.append(page)
        return page

    async def cookies(self, urls: list[str] | None = None) -> list[dict]:
        self.cookies_requested.append(urls or [])
        return []

    async def add_cookies(self, cookies: list[dict]) -> None:
        self.cookies_added.append(cookies)

    async def new_cdp_session(self, page) -> Any:
        return self._cdp_session


class FakeBrowser:
    def __init__(self, context: FakeContext | None = None):
        self.context = context or FakeContext()
        self.contexts = [self.context]

    async def close(self) -> None:
        return None


class FakePlaywright:
    def __init__(self, browser: FakeBrowser | None = None):
        self.browser = browser or FakeBrowser()

    async def stop(self) -> None:
        return None


@pytest.fixture()
def fake_page() -> FakePage:
    return FakePage()


@pytest.fixture()
def fake_context() -> FakeContext:
    return FakeContext()


@pytest.fixture()
def fake_browser() -> FakeBrowser:
    return FakeBrowser()


@pytest.fixture()
def session(cfg):
    from backend.services.agent.session.event import EventHub
    from backend.services.agent.session.model import AgentSession

    return AgentSession(id="s1", crawler_id="c", title="t", hub=EventHub())


class FakeCdpMgr:
    """CDPManager 的最小假对象, 供各频道(Console/Network/DOM/Storage)测试。"""

    def __init__(self):
        from backend.services.cdp import Channel, CDPManager

        self.channels: dict[str, Channel] = {}
        self.handlers: list[Any] = []
        self.session = FakeCdpSession()
        self.primary_session = self.session
        self._oid_session: dict[str, str] = {}

    def register_channel(self, handler: Any, max_history: int = 500) -> Any:
        from backend.services.cdp import Channel

        channel = Channel(handler.name, max_history)
        self.channels[handler.name] = channel
        self.handlers.append(handler)
        setattr(self, handler.name, handler)
        return channel

    def channel(self, name: str) -> Any:
        return self.channels[name]

    def primary(self) -> Any:
        return self.primary_session

    def session_for(self, ws_url: str | None) -> Any:
        return self.primary_session

    def remote_item(self, session: Any, obj: dict) -> dict:
        from backend.services.cdp import CDPManager

        return CDPManager.remote_item(self, session, obj)

    def _stack_text(self, stack: dict | None) -> str | None:
        from backend.services.cdp import CDPManager

        return CDPManager._stack_text(stack)

    def _remote_str(self, arg: dict) -> str:
        from backend.services.cdp import CDPManager

        return CDPManager._remote_str(arg)

    async def get_properties(self, object_id: str, timeout: float = 5.0) -> dict:
        return {"ok": True, "props": []}

    async def evaluate(self, expression: str, timeout: float = 5.0) -> dict:
        session = self.primary()
        if session is None:
            return {"ok": False, "error": "no session"}
        try:
            resp = await session.command(
                "Runtime.evaluate", {"expression": expression}, timeout=timeout
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        result = resp.get("result") or {}
        if result.get("exceptionDetails"):
            return {"ok": False, "error": "exception"}
        return {"ok": True, "item": self.remote_item(session, result.get("result") or {})}


class FakeCdpSession:
    """CDPSession 假对象: 记录 command 调用并按配置返回结果。"""

    def __init__(self, responses: dict | None = None):
        self.ws_url = "ws://fake"
        self.responses = responses or {}
        self.command_calls: list[tuple[str, dict | None]] = []
        self.group_depth = 0

    async def command(self, method: str, params: dict | None = None, timeout: float = 5.0):
        self.command_calls.append((method, params))
        if method in self.responses:
            return self.responses[method]
        if isinstance(self.responses, dict) and "default" in self.responses:
            return self.responses["default"]
        return {"id": 1, "result": {}}


@pytest.fixture()
def fake_cdp_mgr() -> FakeCdpMgr:
    return FakeCdpMgr()


# ---------------------------------------------------------------------------
# 路由测试辅助: FakeStream / FakeChannel / FakeWS / FakeAgentManager / 应用工厂
# ---------------------------------------------------------------------------


class FakeChannel:
    """极简频道假对象: attach 时在运行事件循环上定时推送测试消息。"""

    def __init__(self, messages=None, interval: float = 0.02):
        self.messages = messages or ["hello"]
        self.interval = interval
        self._subs = []

    async def attach(self):
        from backend.services.cdp import Subscriber

        sub = Subscriber()
        self._subs.append(sub)
        loop = asyncio.get_running_loop()

        async def _pump():
            try:
                await asyncio.sleep(self.interval)
                for _msg in self.messages:
                    for s in list(self._subs):
                        s.push(_msg)
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

        loop.create_task(_pump())
        return sub

    async def detach(self, sub) -> None:
        try:
            self._subs.remove(sub)
        except ValueError:
            pass


class FakeCapture:
    """ScreenCapture 的最小假对象: attach 后自动推一帧。"""

    def __init__(self, frame: bytes | None = None, ts: float | None = None):
        self.frame = frame if frame is not None else b"\xff\xd8fake-jpeg"
        self._ts = ts if ts is not None else time.time()
        self.latest: tuple[bytes, float] | None = None
        self.subs = []

    def attach(self, sub) -> None:
        self.subs.append(sub)
        loop = asyncio.get_running_loop()

        async def _push():
            try:
                await asyncio.sleep(0.03)
                self.latest = (self.frame, self._ts)
                for s in list(self.subs):
                    s.push((self.frame, self._ts))
            except asyncio.CancelledError:
                pass

        loop.create_task(_push())

    def detach(self, sub) -> None:
        try:
            self.subs.remove(sub)
        except ValueError:
            pass

    def push_frame(self, frame: bytes | None = None, ts: float | None = None) -> None:
        f = frame if frame is not None else self.frame
        t = ts if ts is not None else time.time()
        self.latest = (f, t)
        for s in list(self.subs):
            s.push((f, t))


class FakeStorage:
    def __init__(self):
        self.calls = []
        self.channel = FakeChannel()

    async def origin(self):
        self.calls.append("origin")
        return {"ok": True, "origin": "https://example.com"}

    async def items(self, origin, session=False):
        self.calls.append(("items", origin, session))
        return {"ok": True, "items": [{"key": "k", "value": "v"}]}

    async def set_item(self, origin, session, key, value):
        self.calls.append(("set_item", origin, session, key, value))
        return {"ok": True}

    async def remove_item(self, origin, session, key):
        self.calls.append(("remove_item", origin, session, key))
        return {"ok": True}

    async def cookies(self, origin):
        self.calls.append(("cookies", origin))
        return {"ok": True, "cookies": []}

    async def set_cookie(self, origin, name, value, **kwargs):
        self.calls.append(("set_cookie", origin, name, value, kwargs))
        return {"ok": True}

    async def delete_cookie(self, origin, name):
        self.calls.append(("delete_cookie", origin, name))
        return {"ok": True}

    async def idb_databases(self, origin):
        self.calls.append(("idb_databases", origin))
        return {"ok": True, "databases": ["db1"]}

    async def idb_stores(self, origin, database):
        self.calls.append(("idb_stores", origin, database))
        return {"ok": True, "stores": []}

    async def idb_data(self, origin, database, store, skip=0, count=50):
        self.calls.append(("idb_data", origin, database, store, skip, count))
        return {"ok": True, "rows": [], "has_more": False}


class FakeStream:
    """BrowserStream 的最小假对象, 供路由测试使用。"""

    def __init__(self, cfg=None):
        from backend.config import Config

        self.cfg = cfg or Config()
        self.capture = FakeCapture()

        class _Network:
            channel = FakeChannel()

            async def body(self, request_id):
                return {"ok": True, "body": "text", "base64_encoded": False}

            async def clear(self):
                return {"ok": True}

        class _Dom:
            channel = FakeChannel()

            async def tree(self):
                return {"ok": True, "root": {"id": 1, "t": 9}}

            async def box_model(self, backend_node_id):
                return {"ok": True, "box": {"x": 0, "y": 0, "w": 10, "h": 10}}

        class _Console:
            channel = FakeChannel()

        self.console = _Console()
        self.network = _Network()
        self.dom = _Dom()
        self.storage = FakeStorage()
        self.cdp = FakeCdpMgr()
        self.run_code_calls = []
        self.saved = []

    # ---- control 路由用 ----

    async def status(self):
        return {
            "uptime": 1.0,
            "error": None,
            "xvfb": True,
            "chrome": True,
            "chrome_cdp": "http://127.0.0.1:9222",
            "capture": {
                "running": True,
                "error": None,
                "viewers": 0,
                "fps": 30.0,
                "frames_total": 10,
                "last_frame_age_ms": 1.0,
            },
            "cdp": {"targets": 0, "connections": 0, "subscribers": {}, "history": 0},
            "pages": [],
        }

    async def _cdp_pages(self):
        return [{"id": "p1", "url": "https://example.com", "title": "Example"}]

    async def navigate(self, url, new_page=False):
        if url == "http://fail":
            from backend.services.browser import BrowserError

            raise BrowserError("导航失败: boom")
        return {"url": url, "title": "Title"}

    async def screenshot(self):
        return b"\x89PNG\r\n\x1a\nfake"

    async def screenshot_element(self, selector: str) -> bytes:
        return b"fake-element-png"

    async def run_code(self, code, login_gate=None, restart=True):
        self.run_code_calls.append((code, login_gate))
        if "error" in code:
            return {"ok": False, "output": "", "error": "boom", "saved": []}
        return {"ok": True, "output": "done", "error": "", "saved": []}

    async def restart(self):
        return None


class FakeSession:
    """FakeAgentManager 返回的最小会话对象。"""

    def __init__(self, id, crawler_id, title, status="idle"):
        self.id = id
        self.crawler_id = crawler_id
        self.title = title
        self.status = status


class FakeAgentManager:
    """AgentManager 的最小假对象, 覆盖 agent 路由的全部调用面。"""

    def __init__(self):
        from backend.services.agent.session.event import EventHub

        self.hub = EventHub()
        self.sessions: dict[str, FakeSession] = {}
        self.messages: dict[str, list] = {}
        self.code = ""
        self._n = 0

    def _next(self) -> str:
        self._n += 1
        return f"s{self._n}"

    def default_crawler_id(self):
        return "default"

    async def create_session(self, crawler_id, title):
        if not title or not title.strip():
            raise ValueError("会话标题不能为空")
        cid = crawler_id or "default"
        s = FakeSession(self._next(), cid, title.strip())
        self.sessions[s.id] = s
        self.messages[s.id] = []
        return s

    async def list_sessions(self, crawler_id):
        return [{"session_id": s.id, "crawler_id": s.crawler_id, "title": s.title, "status": s.status} for s in self.sessions.values()]

    async def get_messages(self, crawler_id, session_id):
        return self.messages.get(session_id, [])

    async def send_message(self, crawler_id, session_id, content):
        s = self.sessions.get(session_id)
        if s is None:
            raise KeyError("会话不存在")
        if not content or not content.strip():
            raise ValueError("消息内容不能为空")
        s.status = "running"
        return s

    async def delete_session(self, crawler_id, session_id):
        self.sessions.pop(session_id, None)
        self.messages.pop(session_id, None)

    async def rename_session(self, crawler_id, session_id, title):
        s = self.sessions.get(session_id)
        if s is None:
            raise KeyError("会话不存在")
        if not title or not title.strip():
            raise ValueError("会话标题不能为空")
        s.title = title.strip()
        return s

    async def start(self, task):
        if not task or not task.strip():
            raise ValueError("任务描述不能为空")
        return await self.create_session(None, task.strip())

    async def answer(self, crawler_id, session_id, qid, answers):
        if session_id not in self.sessions:
            raise KeyError("会话不存在")
        if not qid:
            raise ValueError("问题编号不匹配")

    async def finalize_session(self, crawler_id, session_id, status=None):
        return {"ok": True, "session_id": session_id, "status": status or "done", "message_count": 0}

    async def stop(self, crawler_id, session_id):
        if session_id not in self.sessions:
            raise KeyError("会话不存在")
        self.sessions[session_id].status = "cancelled"

    async def login_action(self, crawler_id, session_id, action):
        if session_id not in self.sessions:
            raise KeyError("会话不存在")
        if action not in ("send_code", "refresh_captcha"):
            raise ValueError(f"未知登录动作: {action}")
        return {"ok": True, "message": "done"}

    async def login_answer(self, crawler_id, session_id, qid, answers):
        if session_id not in self.sessions:
            raise KeyError("会话不存在")

    def editor_code(self):
        return self.code

    def set_editor_code(self, code):
        self.code = code


def make_test_app(stream=None, agent=None, run_login=None, cfg=None) -> Any:
    """构建注入假服务的 FastAPI 应用(不运行 lifespan)。"""
    from fastapi import FastAPI

    from backend.config import Config
    from backend.routers import agent as agent_router
    from backend.routers import console as console_router
    from backend.routers import control as control_router
    from backend.routers import lsp as lsp_router
    from backend.routers import stream as stream_router
    from backend.services.agent.run_login import RunLoginManager
    from backend.services.agent.session.event import EventHub

    if cfg is None:
        cfg = Config()
    app = FastAPI()
    app.state.cfg = cfg
    app.state.stream = stream or FakeStream(cfg)
    app.state.agent = agent or FakeAgentManager()
    app.state.run_login = (
        run_login if run_login is not None else RunLoginManager(EventHub())
    )
    app.include_router(console_router.router)
    app.include_router(control_router.router, prefix=cfg.api_prefix)
    app.include_router(lsp_router.router, prefix=cfg.api_prefix)
    app.include_router(stream_router.router, prefix=cfg.api_prefix)
    app.include_router(agent_router.router, prefix=cfg.api_prefix)
    return app


class FakeWS:
    """极简 WebSocket 假对象, 直接调用路由 handler 时使用。"""

    def __init__(self, app: Any = None, query_params: dict | None = None):
        self.app = app
        self.query_params = query_params or {}
        self.accepted = False
        self.sent: list = []
        self._recv: asyncio.Queue = asyncio.Queue()
        self.disconnect_on_recv = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        if self.disconnect_on_recv:
            raise WebSocketDisconnect()
        return await asyncio.wait_for(self._recv.get(), timeout=5)

    def put_text(self, data: str) -> None:
        self._recv.put_nowait(data)
