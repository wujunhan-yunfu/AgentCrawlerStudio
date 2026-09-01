"""backend.services.browser 测试。"""

from __future__ import annotations

import asyncio
import signal

import pytest

from conftest import FakeContext, FakePage


@pytest.fixture()
def stream(cfg):
    from backend.services.browser import BrowserStream

    return BrowserStream(cfg)


# --------------------------------------------------------------------------- 基础/子进程探测


def test_browser_error():
    from backend.services.browser import BrowserError

    e = BrowserError("boom")
    assert str(e) == "boom"


def test_client_lazy(stream):
    c1 = stream._client()
    assert stream._http is c1
    assert stream._client() is c1


def test_xvfb_on_display_no_lock(stream, monkeypatch, tmp_path):
    import backend.services.browser as bmod

    class FakePath:
        _contents = {}

        def __init__(self, path):
            self.path = str(path)

        def exists(self):
            if self.path == "/tmp/.X99-lock":
                return False
            return True

        def read_text(self, encoding="utf-8"):
            return "12345"

        def read_bytes(self):
            return b""

    monkeypatch.setattr(bmod, "Path", FakePath)
    assert stream._xvfb_on_display() is None


def test_xvfb_on_display_lock_invalid(stream, monkeypatch):
    import backend.services.browser as bmod

    class FakePath:
        def __init__(self, path):
            self.path = str(path)

        def exists(self):
            return True

        def read_text(self, encoding="utf-8"):
            if self.path == "/tmp/.X99-lock":
                return "not-a-number"
            return ""

        def read_bytes(self):
            return b"Xvfb"

    monkeypatch.setattr(bmod, "Path", FakePath)
    assert stream._xvfb_on_display() is None


def test_xvfb_on_display_pid_invalid(stream, monkeypatch):
    import backend.services.browser as bmod

    class FakePath:
        def __init__(self, path):
            self.path = str(path)

        def exists(self):
            return True

        def read_text(self, encoding="utf-8"):
            return "-5"

        def read_bytes(self):
            return b""

    monkeypatch.setattr(bmod, "Path", FakePath)
    assert stream._xvfb_on_display() is None


def test_xvfb_on_display_cmdline_error(stream, monkeypatch):
    import backend.services.browser as bmod

    class FakePath:
        def __init__(self, path):
            self.path = str(path)

        def exists(self):
            return True

        def read_text(self, encoding="utf-8"):
            return "12345"

        def read_bytes(self):
            raise OSError("no proc")

    monkeypatch.setattr(bmod, "Path", FakePath)
    assert stream._xvfb_on_display() is None


def test_xvfb_on_display_found(stream, monkeypatch):
    import backend.services.browser as bmod

    class FakePath:
        def __init__(self, path):
            self.path = str(path)

        def exists(self):
            return True

        def read_text(self, encoding="utf-8"):
            return "12345"

        def read_bytes(self):
            return b"/usr/bin/Xvfb :99 -screen 0 1280x800x24"

    monkeypatch.setattr(bmod, "Path", FakePath)
    assert stream._xvfb_on_display() == 12345


def test_xvfb_on_display_wrong_proc(stream, monkeypatch):
    import backend.services.browser as bmod

    class FakePath:
        def __init__(self, path):
            self.path = str(path)

        def exists(self):
            return True

        def read_text(self, encoding="utf-8"):
            return "12345"

        def read_bytes(self):
            return b"/bin/bash"

    monkeypatch.setattr(bmod, "Path", FakePath)
    assert stream._xvfb_on_display() is None


def test_cleanup_stale_chrome(cfg, monkeypatch):
    from backend.services.browser import BrowserStream
    from backend.config import PROJECT_ROOT

    killed = []
    marker = f"--user-data-dir={PROJECT_ROOT}/".encode()

    class FakeEntry:
        def __init__(self, name):
            self.name = name

        def __truediv__(self, other):
            return FakeChild(self.name, str(other))

    class FakeChild:
        def __init__(self, pid, name):
            self.pid = pid
            self.name = name

        def read_bytes(self):
            if self.name == "cmdline":
                if self.pid == "100":
                    return marker + b"--remote-debugging-port=9222 --type=renderer"
                if self.pid in ("200", "300"):
                    return marker + b"--remote-debugging-port=9222"
                if self.pid == "400":
                    return b"other-cmd"
                if self.pid == "999":
                    return b"/bin/sh"
                return b""
            return b""

        def read_text(self, errors="replace"):
            # /proc/<pid>/stat: pid (comm) state PPID pgrp ...
            return f"0 (process) S 999 0 0 0 0"

    class FakePath:
        def __init__(self, path):
            self.path = str(path)

        def __truediv__(self, other):
            return FakeEntry(f"{self.path}/{other}".rsplit("/", 1)[-1]) if self.path == "/proc" else FakeEntry(str(other))

        def iterdir(self):
            return iter([FakeEntry("100"), FakeEntry("200"), FakeEntry("300"), FakeEntry("400")])

    import backend.services.browser as bmod

    monkeypatch.setattr(bmod, "Path", FakePath)
    monkeypatch.setattr(bmod.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    stream = BrowserStream(cfg)
    stream._cleanup_stale_chrome()
    # 100 是 renderer 子进程 -> 跳过; 200/300 父进程是 999(非 backend.main) -> 杀掉
    assert 200 in [pid for pid, _ in killed]
    assert 300 in [pid for pid, _ in killed]
    # 400 无标记 -> 不杀
    assert 400 not in [pid for pid, _ in killed]


def test_cleanup_stale_chrome_alive_parent(cfg, monkeypatch):
    from backend.services.browser import BrowserStream

    killed = []

    class FakeEntry:
        def __init__(self, name):
            self.name = name

        def __truediv__(self, other):
            return FakeChild(self.name, str(other))

    class FakeChild:
        def __init__(self, pid, name):
            self.pid = pid
            self.name = name

        def read_bytes(self):
            if self.name == "cmdline":
                return b"--user-data-dir=/root/xvfb_test/ --remote-debugging-port=9222"
            return b""

        def read_text(self, errors="replace"):
            return "0 (p) S 500"

    class FakePath:
        def __init__(self, path):
            self.path = str(path)

        def iterdir(self):
            return iter([FakeEntry("500")])

    class FakeParent:
        def __init__(self, path):
            self.path = path

        def read_bytes(self):
            if str(self.path) == "/proc/500/cmdline":
                return b"backend.main"
            return b""

    import backend.services.browser as bmod

    monkeypatch.setattr(bmod, "Path", lambda p: FakePath(p) if str(p) == "/proc" else FakeParent(p))
    monkeypatch.setattr(bmod.os, "kill", lambda pid, sig: killed.append(pid))
    stream = BrowserStream(cfg)
    stream._cleanup_stale_chrome()
    assert killed == []


# --------------------------------------------------------------------------- Xvfb / Chrome 启动


async def test_start_xvfb_reuse(stream, monkeypatch):
    import backend.services.browser as bmod

    monkeypatch.setattr(stream, "_xvfb_on_display", lambda: 12345)
    await stream._start_xvfb()
    assert stream.xvfb is None
    assert stream.xvfb_pid == 12345
    assert stream.xvfb_owned is False


async def test_start_xvfb_spawn(stream, monkeypatch):
    import backend.services.browser as bmod

    class FakeProc:
        pid = 777

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, *a):
            pass

    monkeypatch.setattr(bmod.subprocess, "Popen", lambda *a, **kw: FakeProc())
    monkeypatch.setattr(stream, "_xvfb_on_display", lambda: None)

    class FakeXPath:
        def __init__(self, path):
            self.path = str(path)

        def exists(self):
            return str(self.path).startswith("/tmp/.X11-unix/")

    monkeypatch.setattr(bmod, "Path", FakeXPath)
    await stream._start_xvfb()
    assert stream.xvfb_pid == 777
    assert stream.xvfb_owned is True


async def test_start_xvfb_fail(stream, monkeypatch):
    import backend.services.browser as bmod

    class FakeProc:
        pid = 777

        def poll(self):
            return 1

    monkeypatch.setattr(bmod.subprocess, "Popen", lambda *a, **kw: FakeProc())
    monkeypatch.setattr(stream, "_xvfb_on_display", lambda: None)

    class FakeXPath:
        def __init__(self, path):
            self.path = str(path)

        def exists(self):
            return False

    monkeypatch.setattr(bmod, "Path", FakeXPath)
    with pytest.raises(RuntimeError, match="Xvfb 启动失败"):
        await stream._start_xvfb()


async def test_start_xvfb_socket_timeout(stream, monkeypatch):
    import backend.services.browser as bmod

    class FakeProc:
        pid = 777

        def poll(self):
            return None

    monkeypatch.setattr(bmod.subprocess, "Popen", lambda *a, **kw: FakeProc())
    monkeypatch.setattr(stream, "_xvfb_on_display", lambda: None)

    class FakeXPath:
        def __init__(self, path):
            self.path = str(path)

        def exists(self):
            return False

    monkeypatch.setattr(bmod, "Path", FakeXPath)
    monkeypatch.setattr(bmod.asyncio, "sleep", _no_sleep)
    with pytest.raises(RuntimeError, match="等待 Xvfb socket 超时"):
        await stream._start_xvfb()


async def test_wait_cdp_ready(stream, monkeypatch):
    import backend.services.browser as bmod

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def get(self, url):
            self.calls += 1
            return _Resp(200)

    monkeypatch.setattr(stream, "_client", lambda: FakeClient())
    await stream._wait_cdp()
    assert stream.chrome is None


async def test_wait_cdp_http_error_then_ok(stream, monkeypatch):
    import httpx

    client = None

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def get(self, url):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("conn refused")
            return _Resp(404 if self.calls == 2 else 200)

    class FakeProc:
        def poll(self):
            return None

    monkeypatch.setattr(stream, "_client", lambda: client)
    client = FakeClient()
    stream.chrome = FakeProc()
    await stream._wait_cdp()
    assert client.calls == 3


async def test_wait_cdp_chrome_exit(stream, monkeypatch):
    import httpx

    class FakeClient:
        async def get(self, url):
            raise httpx.ConnectError("offline")

    class FakeProc:
        def poll(self):
            return 1

    monkeypatch.setattr(stream, "_client", lambda: FakeClient())
    stream.chrome = FakeProc()
    with pytest.raises(RuntimeError, match="Chrome 进程提前退出"):
        await stream._wait_cdp()


async def test_wait_cdp_timeout(stream, monkeypatch):
    import backend.services.browser as bmod
    import httpx

    class FakeClient:
        async def get(self, url):
            raise httpx.ConnectError("offline")

    class FakeProc:
        def poll(self):
            return None

    monkeypatch.setattr(stream, "_client", lambda: FakeClient())
    monkeypatch.setattr(bmod.asyncio, "sleep", _no_sleep)
    stream.chrome = FakeProc()
    with pytest.raises(RuntimeError, match="Chrome CDP 端口就绪超时"):
        await stream._wait_cdp()


async def test_start_chrome(stream, monkeypatch):
    import backend.services.browser as bmod

    monkeypatch.setattr(bmod, "find_chrome", lambda: "/usr/bin/chrome")
    monkeypatch.setattr(bmod.tempfile, "mkdtemp", lambda **kw: "/fake/profile")

    class FakeProc:
        def __init__(self, args, **kw):
            self.args = args

        def poll(self):
            return None

    monkeypatch.setattr(bmod.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(stream, "_wait_cdp", _AsyncNoop)

    await stream._start_chrome()
    assert stream._profile_dir == "/fake/profile"
    assert "--no-sandbox" in stream.chrome_args
    assert stream.chrome is not None
    assert stream._env["DISPLAY"] == ":99"


# --------------------------------------------------------------------------- 生命周期


async def test_start_full(stream, monkeypatch):
    import backend.services.browser as bmod

    monkeypatch.setattr(bmod, "find_free_port", lambda p: 9223)
    monkeypatch.setattr(bmod.httpx, "AsyncClient", lambda *a, **kw: "client")
    monkeypatch.setattr(stream, "_cleanup_stale_chrome", _SyncNoop)
    monkeypatch.setattr(stream, "_start_xvfb", _AsyncNoop)
    monkeypatch.setattr(stream, "_start_chrome", _AsyncNoop)
    monkeypatch.setattr(stream.capture, "start", _AsyncNoop)

    await stream.start()
    assert stream.cfg.cdp_port == 9223
    assert stream.started_at is not None
    assert stream.error is None


async def test_start_failure(stream, monkeypatch):
    import backend.services.browser as bmod

    monkeypatch.setattr(stream, "_start_chrome", _AsyncFail)
    monkeypatch.setattr(stream, "stop", _AsyncNoop)
    await stream.start()
    assert stream.error is not None
    assert "boom" in stream.error


async def test_terminate_alive(stream):
    class FakeProc:
        def __init__(self):
            self.terminated = False
            self.killed = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 0

    proc = FakeProc()
    await stream._terminate(proc)
    assert proc.terminated is True
    assert proc.killed is False


async def test_terminate_timeout(stream):
    import subprocess

    class FakeProc:
        def __init__(self):
            self.waits = 0

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            self.waits += 1
            if timeout is not None and self.waits == 1:
                raise subprocess.TimeoutExpired("p", timeout)
            return 9

    proc = FakeProc()
    await stream._terminate(proc)
    assert proc.waits == 2
    await stream._terminate(None)


async def test_stop(stream, monkeypatch):
    import backend.services.browser as bmod

    stream.chrome = object()
    stream.xvfb = object()
    stream.xvfb_owned = True
    stream._profile_dir = "/fake/profile"
    stream._http = object()
    monkeypatch.setattr(stream, "_terminate", _AsyncNoop)
    monkeypatch.setattr(stream.capture, "stop", _AsyncNoop)
    monkeypatch.setattr(bmod.shutil, "rmtree", lambda *a, **kw: None)

    class FakeHttp:
        async def aclose(self):
            pass

    stream._http = FakeHttp()
    await stream.stop()
    assert stream.chrome is None
    assert stream.xvfb is None
    assert stream._profile_dir is None
    assert stream._http is None


async def test_restart(stream, monkeypatch):
    monkeypatch.setattr(stream, "stop", _AsyncNoop)
    monkeypatch.setattr(stream, "start", _AsyncNoop)
    monkeypatch.setattr(stream, "_sync_broker_connect", _AsyncNoop)
    stream.error = "old"
    await stream.restart()
    assert stream.error is None


async def test_restart_chrome(stream, monkeypatch):
    import backend.services.browser as bmod

    monkeypatch.setattr(stream, "_terminate", _AsyncNoop)
    monkeypatch.setattr(bmod.shutil, "rmtree", lambda *a, **kw: None)
    monkeypatch.setattr(bmod, "find_free_port", lambda p: 9333)
    monkeypatch.setattr(stream, "_start_chrome", _AsyncNoop)
    monkeypatch.setattr(stream, "_sync_broker_connect", _AsyncNoop)
    stream.chrome = object()
    stream._profile_dir = "/fake"
    await stream.restart_chrome()
    assert stream.chrome is None
    assert stream.cfg.cdp_port == 9333


async def test_sync_broker_connect(stream, monkeypatch):
    stream.cdp.rescan = lambda: None
    stream.loop = object()

    async def connect_now():
        pass

    stream.cdp.connect_now = connect_now
    await stream._sync_broker_connect()

    async def connect_now_fail():
        raise RuntimeError("x")

    stream.cdp.connect_now = connect_now_fail
    await stream._sync_broker_connect()


# --------------------------------------------------------------------------- 状态


class FakeProcAlive:
    def poll(self):
        return None


class FakeProcDead:
    def poll(self):
        return 0


def test_alive(stream):
    assert stream._alive(None) is False
    assert stream._alive(FakeProcAlive()) is True
    assert stream._alive(FakeProcDead()) is False


def test_xvfb_alive(stream, monkeypatch):
    stream.xvfb = FakeProcAlive()
    assert stream._xvfb_alive() is True
    stream.xvfb = FakeProcDead()
    monkeypatch.setattr(stream, "_xvfb_on_display", lambda: 123)
    assert stream._xvfb_alive() is True
    monkeypatch.setattr(stream, "_xvfb_on_display", lambda: None)
    assert stream._xvfb_alive() is False


async def test_status(stream, monkeypatch):
    stream.started_at = 1000.0
    stream.chrome = FakeProcAlive()
    stream.capture.status = lambda: {"running": True, "viewers": 0}
    stream.cdp.status = lambda: {"targets": 1}
    monkeypatch.setattr(stream, "_xvfb_alive", lambda: True)
    monkeypatch.setattr(stream, "_cdp_pages", _AsyncNoopList)
    st = await stream.status()
    assert st["xvfb"] is True
    assert st["chrome"] is True
    assert st["chrome_cdp"] == "http://127.0.0.1:9222"
    assert st["uptime"] is not None


async def test_status_no_started(stream, monkeypatch):
    monkeypatch.setattr(stream, "_xvfb_alive", lambda: False)
    monkeypatch.setattr(stream, "_cdp_pages", _AsyncNoopList)
    st = await stream.status()
    assert st["uptime"] is None


async def test_cdp_pages(stream, monkeypatch):
    class FakeClient:
        async def get(self, url):
            return _Resp(200, payload=[
                {"id": "1", "url": "http://a", "title": "A", "type": "page"},
                {"id": "2", "url": "http://b", "title": "B", "type": "other"},
            ])

    monkeypatch.setattr(stream, "_client", lambda: FakeClient())
    stream.chrome = FakeProcAlive()
    pages = await stream._cdp_pages()
    assert len(pages) == 1
    assert pages[0]["id"] == "1"


async def test_cdp_pages_not_alive(stream):
    stream.chrome = None
    assert await stream._cdp_pages() == []


async def test_cdp_pages_error(stream, monkeypatch):
    class FakeClient:
        async def get(self, url):
            raise RuntimeError("boom")

    monkeypatch.setattr(stream, "_client", lambda: FakeClient())
    stream.chrome = FakeProcAlive()
    assert await stream._cdp_pages() == []


# --------------------------------------------------------------------------- Playwright 控制


@pytest.fixture()
def fake_pw_bundle(monkeypatch):
    """让 stream._pw 返回固定的假 playwright 对象。"""
    created = {}

    def make_bundle():
        context = FakeContext()
        page = FakePage(url="http://a")
        context._pages.append(page)
        browser = FakeBrowserWithContext(context)

        class FakePw:
            async def start(self):
                return FakePwRunner(browser)

        class FakePwRunner:
            def __init__(self, browser):
                self.chromium = FakeChromium(browser)
                self.stopped = False

            async def stop(self):
                self.stopped = True

        class FakeChromium:
            def __init__(self, browser):
                self._browser = browser

            async def connect_over_cdp(self, url):
                created["browser"] = browser
                return browser

        created["pw"] = FakePw()
        return created["pw"]

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: make_bundle()
    )
    return created


class FakeBrowserWithContext:
    def __init__(self, context):
        self.contexts = [context]
        self.closed = False

    async def close(self):
        self.closed = True


async def test_navigate(stream, fake_pw_bundle):
    res = await stream.navigate("http://example.com")
    assert res["url"] == "http://example.com"
    assert res["title"] == "title"


async def test_navigate_error(stream, monkeypatch):
    from backend.services.browser import BrowserError

    class BoomChromium:
        async def connect_over_cdp(self, url):
            raise RuntimeError("cdp down")

    class BoomPw:
        async def start(self):
            class R:
                chromium = BoomChromium()

            return R()

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: BoomPw())
    with pytest.raises(BrowserError, match="导航失败"):
        await stream.navigate("http://x")


async def test_screenshot(stream, fake_pw_bundle):
    data = await stream.screenshot()
    assert data == b"fake-png"


async def test_screenshot_element(stream, fake_pw_bundle):
    data = await stream.screenshot_element("#captcha")
    assert data == b"fake-element-png"


# --------------------------------------------------------------------------- run_code / _exec_async


async def test_exec_async_simple():
    from backend.services.browser import BrowserStream

    env = {"__builtins__": {}, "acc": []}
    await BrowserStream._exec_async("acc.append(1)\nacc.append(2)", env)
    assert env["acc"] == [1, 2]


async def test_exec_async_blank():
    from backend.services.browser import BrowserStream

    env = {"__builtins__": {}}
    await BrowserStream._exec_async("   \n\n", env)


async def test_exec_async_top_level_await():
    from backend.services.browser import BrowserStream

    async def _target():
        return 42

    env = {"__builtins__": {}, "_target": _target, "acc": []}
    await BrowserStream._exec_async("acc.append(await _target())", env)
    assert env["acc"] == [42]


async def test_run_code(stream, monkeypatch, fake_pw_bundle):
    import backend.services.crawler as cmod

    calls = []

    async def fake_restart_chrome():
        calls.append("restart")

    monkeypatch.setattr(stream, "restart_chrome", fake_restart_chrome)
    monkeypatch.setattr(stream, "_pw", _make_fake_pw)

    env_obj_calls = {}

    class FakeCrawlerEnv:
        def __init__(self, cfg, page, context=None, login_gate=None):
            env_obj_calls["page"] = page
            env_obj_calls["context"] = context

        async def reset_saved(self):
            pass

        async def save_page(self):
            return "/tmp/saved/page.html"

        async def save_content(self, data, fmt="txt"):
            return "/tmp/saved/content.txt"

        def limit_items(self, data, n=None):
            return data

        async def get_login_ticket(self, host):
            return None

        async def set_login_ticket(self, ticket, host):
            return ticket

        async def page_login(self, method, **kw):
            return {"ok": True}

        async def capture_login_state(self):
            return {}

        async def restore_login_state(self, state):
            return "ok"

        def saved_items(self):
            return [{"id": "1", "name": "x"}]

    monkeypatch.setattr(cmod, "CrawlerEnv", FakeCrawlerEnv)

    res = await stream.run_code("print('hello')")
    assert res["ok"] is True
    assert "hello" in res["output"]
    assert len(res["saved"]) == 1


async def test_run_code_restart_failure(stream, monkeypatch):
    async def fail():
        raise RuntimeError("chrome dead")

    monkeypatch.setattr(stream, "restart_chrome", fail)
    res = await stream.run_code("x = 1")
    assert res["ok"] is False
    assert "chrome dead" in res["error"]


async def test_run_code_no_restart_without_chrome(stream, monkeypatch):
    async def fake_restart():
        raise RuntimeError("no chrome")

    monkeypatch.setattr(stream, "restart_chrome", fake_restart)
    res = await stream.run_code("x = 1", restart=False)
    assert res["ok"] is False


async def test_run_code_existing_error(stream, monkeypatch):
    stream.error = "prev error"
    res = await stream.run_code("x = 1")
    assert res["ok"] is False
    assert res["error"] == "prev error"


async def test_run_code_exec_error(stream, monkeypatch):
    import backend.services.crawler as cmod

    monkeypatch.setattr(stream, "restart_chrome", _AsyncNoop)
    monkeypatch.setattr(stream, "_pw", _make_fake_pw)
    monkeypatch.setattr(cmod, "CrawlerEnv", _FullFakeEnv)
    res = await stream.run_code("raise ValueError('user code boom')")
    assert res["ok"] is False
    assert "user code boom" in res["error"]


async def test_run_code_login_cancelled(stream, monkeypatch):
    """用户取消登录 → 脚本终止并返回明确结果, 而不是异常堆栈。"""
    from backend.services.agent.login import LoginCancelled

    import backend.services.crawler as cmod

    class CancelEnv(_FullFakeEnv):
        async def page_login(self, method, **kw):
            raise LoginCancelled("用户取消登录")

        def saved_items(self):
            return [{"id": "1", "name": "x"}]

    monkeypatch.setattr(stream, "restart_chrome", _AsyncNoop)
    monkeypatch.setattr(stream, "_pw", _make_fake_pw)
    monkeypatch.setattr(cmod, "CrawlerEnv", CancelEnv)
    res = await stream.run_code("await page_login(method='qr')")
    assert res["ok"] is False
    assert res["error"] == "用户取消登录"
    assert "Traceback" not in res["error"]
    assert len(res["saved"]) == 1


# --------------------------------------------------------------------------- helpers


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


async def _make_fake_pw():
    from conftest import FakeBrowser, FakeContext, FakePage

    context = FakeContext()
    page = FakePage(url="http://a")
    context._pages.append(page)
    browser = FakeBrowser(context)
    return FakePwStub(browser), browser


class FakePwStub:
    def __init__(self, browser):
        self._browser = browser

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def chromium(self):
        return self


class _FullFakeEnv:
    """带全部注入属性的 FakeCrawlerEnv, 供 run_code 测试复用。"""

    def __init__(self, cfg, page, context=None, login_gate=None):
        pass

    async def reset_saved(self):
        pass

    async def save_page(self):
        return "/tmp/saved/page.html"

    async def save_content(self, data, fmt="txt"):
        return "/tmp/saved/content.txt"

    def limit_items(self, data, n=None):
        return data

    async def get_login_ticket(self, host):
        return None

    async def set_login_ticket(self, ticket, host):
        return ticket

    async def page_login(self, method, **kw):
        return {"ok": True}

    async def capture_login_state(self):
        return {}

    async def restore_login_state(self, state):
        return "ok"

    def saved_items(self):
        return []


async def _AsyncNoop(*args, **kwargs):
    return None


def _SyncNoop(*args, **kwargs):
    return None


async def _AsyncFail(*args, **kwargs):
    raise RuntimeError("boom")


async def _AsyncNoopList(*args, **kwargs):
    return []


async def _no_sleep(*args, **kwargs):
    return None
