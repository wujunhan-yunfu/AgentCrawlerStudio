"""浏览器链路服务: 管理 Xvfb / Chrome 子进程 + 抓屏 + Playwright 控制(全异步)"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from ..config import PROJECT_ROOT, find_chrome, find_free_port
from .capture import ScreenCapture
from .cdp import CDPManager
from .console import ConsoleChannel
from .dom import DOMChannel
from .network import NetworkChannel
from .storage import StorageChannel


class BrowserError(Exception):
    """服务层异常, 由路由层转换为 HTTP 错误"""


class BrowserStream:
    """管理 Xvfb / Chrome 子进程 + 抓屏(纯 asyncio, 无线程)"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.xvfb: subprocess.Popen | None = None
        self.xvfb_pid: int | None = None
        self.xvfb_owned: bool = False
        self.chrome: subprocess.Popen | None = None
        self.capture = ScreenCapture(cfg)
        self.cdp = CDPManager(cfg)
        self.console = ConsoleChannel(self.cdp)
        self.network = NetworkChannel(self.cdp)
        self.dom = DOMChannel(self.cdp)
        self.storage = StorageChannel(self.cdp)
        self.error: str | None = None
        self.started_at: float | None = None
        self.chrome_args: list[str] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self._env: dict = {}
        self._profile_dir: str | None = None
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=1.0)
        return self._http

    # ---------- 子进程 ----------
    def _xvfb_on_display(self) -> int | None:
        """返回正在服务配置显示器的存活 Xvfb 进程 PID; 无则返回 None。

        通过 X 显示器锁文件(/tmp/.X<n>-lock)中的 PID + 进程 cmdline 判断,
        可识别上次实例崩溃后遗留、仍持有该显示器的 Xvfb。
        """
        num = self.cfg.display.lstrip(":")
        lock = Path(f"/tmp/.X{num}-lock")
        if not lock.exists():
            return None
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        if pid <= 0:
            return None
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        return pid if b"Xvfb" in cmdline else None

    def _cleanup_stale_chrome(self) -> None:
        """清理上次实例崩溃遗留的本项目 Chrome 主进程。

        遗留的 Chrome 窗口仍占用旧虚拟屏, 会与本次新窗口重叠显示;
        仅处理 user-data-dir 指向本项目且父进程已不在的 Chrome, 不影响存活后端实例。
        """
        marker = f"--user-data-dir={PROJECT_ROOT}/".encode()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmd = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            # 仅处理主进程(--type= 为渲染/GPU 等子进程, 会随主进程一起退出)
            if b"--type=" in cmd:
                continue
            if marker not in cmd or b"--remote-debugging-port" not in cmd:
                continue
            pid = int(entry.name)
            try:
                stat = (entry / "stat").read_text(errors="replace")
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            try:
                parent_cmd = (Path("/proc") / str(ppid) / "cmdline").read_bytes()
            except OSError:
                parent_cmd = b""
            if b"backend.main" in parent_cmd:
                continue  # 存活后端实例自己的 Chrome, 不动
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)

    async def _start_xvfb(self) -> None:
        existing = self._xvfb_on_display()
        if existing is not None:
            # 显示器已被存活 Xvfb 占用(如上次实例遗留), 直接复用, 不重复启动
            self.xvfb = None
            self.xvfb_pid = existing
            self.xvfb_owned = False
            return
        self.xvfb = subprocess.Popen(
            [
                "Xvfb",
                self.cfg.display,
                "-screen",
                "0",
                f"{self.cfg.width}x{self.cfg.height}x24",
                "-nolisten",
                "tcp",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.xvfb_pid = self.xvfb.pid
        self.xvfb_owned = True
        x_sock = Path(f"/tmp/.X11-unix/X{self.cfg.display.lstrip(':')}")
        for _ in range(50):
            if self.xvfb.poll() is not None:
                if x_sock.exists():
                    raise RuntimeError(f"X 显示器 {self.cfg.display} 已被其他进程占用, 新 Xvfb 启动失败")
                raise RuntimeError("Xvfb 启动失败")
            if x_sock.exists():
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("等待 Xvfb socket 超时")

    async def _start_chrome(self) -> None:
        chrome_path = self.cfg.chrome or find_chrome()
        # 每次启动用全新临时配置目录, 不残留历史记录/Cookie(隔离由临时 profile 承担,
        # 不使用 --incognito: 无痕模式下 CDP 默认 context 的 cookie API
        # (context.cookies()/add_cookies)会失效, 见 docs/cookie-detection-optimization.md)
        self._profile_dir = tempfile.mkdtemp(prefix="tmp/chrome-profile-", dir=str(PROJECT_ROOT))
        self.chrome_args = [
            chrome_path,
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            f"--remote-debugging-port={self.cfg.cdp_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self._profile_dir}",
            f"--window-size={self.cfg.width},{self.cfg.height}",
            "--window-position=0,0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-sync",
            "--disable-default-apps",
            "--hide-crash-restore-bubble",
            "--disable-infobars",
            "--mute-audio",
            "--hide-scrollbars",
            "--force-color-profile=srgb",
            "--force-device-scale-factor=1",
        ]

        self._env = {**os.environ, "DISPLAY": self.cfg.display}
        self.chrome = subprocess.Popen(
            self.chrome_args,
            env=self._env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await self._wait_cdp()

    async def _wait_cdp(self) -> None:
        url = f"http://127.0.0.1:{self.cfg.cdp_port}/json/version"
        for _ in range(100):
            try:
                resp = await self._client().get(url)
            except httpx.HTTPError:
                resp = None
            if resp is not None and resp.status_code < 400:
                return
            if self.chrome is not None and self.chrome.poll() is not None:
                raise RuntimeError("Chrome 进程提前退出")
            await asyncio.sleep(0.2)
        raise RuntimeError("Chrome CDP 端口就绪超时")

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        self.started_at = time.time()
        try:
            self.cfg.cdp_port = find_free_port(self.cfg.cdp_port)
            self._http = httpx.AsyncClient(timeout=1.0)
            await asyncio.to_thread(self._cleanup_stale_chrome)
            await self._start_xvfb()
            await self._start_chrome()
            await self.capture.start()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            await self.stop()

    async def _terminate(self, proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait, 5)

    async def stop(self) -> None:
        await self.capture.stop()
        await self._terminate(self.chrome)
        self.chrome = None
        # 仅终止本次启动的 Xvfb; 复用的既有 Xvfb(上次实例遗留)留给其他进程共享
        if self.xvfb_owned:
            await self._terminate(self.xvfb)
        self.xvfb = None
        self.xvfb_pid = None
        self.xvfb_owned = False
        # 清理本次的临时配置目录, 下次启动重新生成
        if self._profile_dir is not None:
            await asyncio.to_thread(shutil.rmtree, self._profile_dir, True)
            self._profile_dir = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def restart(self) -> None:
        await self.stop()
        self.error = None
        self.capture.error = None
        await self.start()
        await self._sync_broker_connect()

    async def _sync_broker_connect(self) -> None:
        """等 CDP 监听连上新浏览器(消除 run_code 首屏事件竞态)"""
        self.cdp.rescan()
        if self.loop is not None:
            try:
                await self.cdp.connect_now()
            except Exception:  # noqa: BLE001
                pass

    async def restart_chrome(self) -> None:
        """仅重启 Chrome(保留 Xvfb/抓屏), 每次执行代码前得到全新无痕浏览器"""
        await self._terminate(self.chrome)
        self.chrome = None
        if self._profile_dir is not None:
            await asyncio.to_thread(shutil.rmtree, self._profile_dir, True)
            self._profile_dir = None
        self.cfg.cdp_port = find_free_port(self.cfg.cdp_port)
        await self._start_chrome()
        await self._sync_broker_connect()

    # ---------- 状态 ----------
    def _alive(self, proc: subprocess.Popen | None) -> bool:
        return proc is not None and proc.poll() is None

    def _xvfb_alive(self) -> bool:
        if self._alive(self.xvfb):
            return True
        # 兼容上次实例崩溃遗留、仍持有配置显示器的 Xvfb
        return self._xvfb_on_display() is not None

    async def status(self) -> dict:
        return {
            "uptime": round(time.time() - self.started_at, 1) if self.started_at else None,
            "error": self.error,
            "xvfb": self._xvfb_alive(),
            "chrome": self._alive(self.chrome),
            "chrome_cdp": f"http://127.0.0.1:{self.cfg.cdp_port}",
            "capture": self.capture.status(),
            "cdp": self.cdp.status(),
            "pages": await self._cdp_pages(),
        }

    async def _cdp_pages(self) -> list[dict]:
        if not self._alive(self.chrome):
            return []
        try:
            resp = await self._client().get(f"http://127.0.0.1:{self.cfg.cdp_port}/json")
            resp.raise_for_status()
            return [
                {"id": t.get("id"), "url": t.get("url"), "title": t.get("title")}
                for t in resp.json()
                if t.get("type") == "page"
            ]
        except Exception:  # noqa: BLE001
            return []

    # ---------- Playwright 控制 ----------
    async def _pw(self):
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.cfg.cdp_port}"
        )
        return pw, browser

    async def navigate(self, url: str, new_page: bool = False) -> dict:
        pw = None
        browser = None
        try:
            pw, browser = await self._pw()
            context = browser.contexts[0]
            if new_page or not context.pages:
                page = await context.new_page()
            else:
                page = context.pages[0]
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return {"url": page.url, "title": await page.title()}
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(f"导航失败: {exc}") from exc
        finally:
            await self._close_pw(pw, browser)

    async def screenshot(self) -> bytes:
        pw = None
        browser = None
        try:
            pw, browser = await self._pw()
            context = browser.contexts[0]
            if not context.pages:
                page = await context.new_page()
            else:
                page = context.pages[0]
            return await page.screenshot()
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(f"截图失败: {exc}") from exc
        finally:
            await self._close_pw(pw, browser)

    async def screenshot_element(self, selector: str) -> bytes:
        """对当前页面指定选择器元素截图(用于图形验证码等)。"""
        pw = None
        browser = None
        try:
            pw, browser = await self._pw()
            context = browser.contexts[0]
            if not context.pages:
                page = await context.new_page()
            else:
                page = context.pages[0]
            return await page.locator(selector).first.screenshot()
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(f"元素截图失败: {exc}") from exc
        finally:
            await self._close_pw(pw, browser)

    async def _close_pw(self, pw, browser) -> None:
        """关闭 playwright 会话(幂等, 异常静默)。"""
        try:
            if browser is not None:
                await browser.close()
            if pw is not None:
                await pw.stop()
        except Exception:  # noqa: BLE001
            pass

    async def run_code(self, code: str, login_gate: Any = None,
                       restart: bool = True) -> dict:
        """执行用户编写的 Playwright 代码(async 风格)。

        restart=True(默认)时执行前先重启 Chrome 得到全新无痕浏览器(保留 Xvfb 与抓屏);
        restart=False 时复用当前已运行的浏览器(不重启), 可保留登录态/已打开页面做
        增量操作(如登录后探查凭据、注入测试), 便于同一浏览器内连续探查。
        代码环境中提供 page / context / browser 对象, 同时注入爬虫默认函数
        save_page / save_content / limit_items / get_login_ticket / set_login_ticket /
        page_login, print 输出会被捕获, 保存的内容(save_page/save_content)随结果返回。
        脚本为 async 风格: 使用 page/context/browser 及内置函数时需 await
        (如 `await page.goto(url)`、`await save_page()`), 顶层 await 受支持。
        login_gate: 爬虫 Agent 会话注入的登录桥, 供 page_login 与用户交互;
        非 Agent 调用(前端直接运行)时不传, page_login 会抛明确错误。
        代码运行在受限环境: 除 save_content / save_page 外的一切文件读写
        (open / os / pathlib / shutil / subprocess / io 等)均被禁用。
        开发测试模式(默认开启)下 save_page/save_content 自动限制数据量,
        limit_items(data, n) 可用于限制遍历长度; 上线时加 --no-dev-limit 取消。
        """
        import traceback
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        from .crawler import CrawlerEnv
        from .sandbox import safe_builtins

        if restart:
            try:
                await self.restart_chrome()
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                return {"ok": False, "output": "", "error": self.error, "saved": []}
        elif not self._alive(self.chrome):
            # 不重启但当前无 Chrome 实例时, 仍需启动一个
            try:
                await self.restart_chrome()
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                return {"ok": False, "output": "", "error": self.error, "saved": []}
        if self.error:
            return {"ok": False, "output": "", "error": self.error, "saved": []}

        out = StringIO()
        pw = None
        browser = None
        env_obj: CrawlerEnv | None = None
        try:
            pw, browser = await self._pw()
            context = browser.contexts[0]
            if not context.pages:
                page = await context.new_page()
            else:
                page = context.pages[0]
            env_obj = CrawlerEnv(
                self.cfg, page, context=context, login_gate=login_gate
            )
            await env_obj.reset_saved()
            env = {
                "page": page,
                "context": context,
                "browser": browser,
                "save_page": env_obj.save_page,
                "save_content": env_obj.save_content,
                "limit_items": env_obj.limit_items,
                "get_login_ticket": env_obj.get_login_ticket,
                "set_login_ticket": env_obj.set_login_ticket,
                "page_login": env_obj.page_login,
                "capture_login_state": env_obj.capture_login_state,
                "restore_login_state": env_obj.restore_login_state,
                "__name__": "__main__",
                "__builtins__": safe_builtins(),
            }
            with redirect_stdout(out), redirect_stderr(out):
                await self._exec_async(code, env)
            return {"ok": True, "output": out.getvalue(), "error": "", "saved": env_obj.saved_items()}
        except Exception:  # noqa: BLE001
            saved = env_obj.saved_items() if env_obj is not None else []
            return {"ok": False, "output": out.getvalue(), "error": traceback.format_exc(), "saved": saved}
        finally:
            try:
                if browser is not None:
                    await browser.close()
                if pw is not None:
                    await pw.stop()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _exec_async(code: str, env: dict) -> None:
        """把用户脚本作为 async 函数包装执行, 支持顶层 await。

        脚本语句会整体缩进包进 `async def __run__()` 中, 使其能直接使用
        `await page.goto(...)` 等顶层 await 写法。
        """
        lines = code.splitlines()
        if not any(line.strip() for line in lines):
            return
        indented = "async def __run__():\n" + "\n".join(
            ("    " + line if line.strip() else line) for line in lines
        )
        namespace: dict = {}
        exec(compile(indented, "<playwright-code>", "exec"), env, namespace)
        await namespace["__run__"]()
