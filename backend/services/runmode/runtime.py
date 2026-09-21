"""独立运行环境(run 模式): 直接启动 Playwright 执行 Mongo 中的 HEAD 代码。

与导出包 RUNTIME_PY 行为一致, 但:
- 代码来自 Mongo(经 render_crawler_module 打包为可 import 的 crawler.py);
- 注入的 save_content 同时触发数据 webhook 收集(见 data_webhook.py);
- 登录凭据读写 MongoDB login_tickets(按 crawler_id 隔离);
- page_login 采用「打印提示 + 轮询页面跳转」, 不依赖前端答复;
- verify_check 为占位实现(不启用自动过验, 与导出包一致)。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sys
import tempfile
import time
import traceback
import uuid
from itertools import islice
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from ...config import Config
from ..exporter import render_crawler_module
from ..save import cap_text_bytes, normalize_fmt, prepare_save
from motor.motor_asyncio import AsyncIOMotorClient

TICKET_COLLECTION = "login_tickets"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class VerificationFailed(Exception):
    """人机验证限次未通过(run 模式下 verify_check 为占位, 不主动抛出)。"""


class _VerifyScope:
    def passed(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {"enabled": False}


def _same_page(a: str, b: str) -> bool:
    """判断两个 URL 是否指向同一页面(scheme+host+path), about:blank 视为不同。"""
    try:
        ua, ub = urlsplit(a or ""), urlsplit(b or "")
    except ValueError:
        return False
    if not ua.hostname or not ub.hostname:
        return False
    return (ua.scheme, ua.hostname, ua.path) == (ub.scheme, ub.hostname, ub.path)


class _MongoTicketStore:
    """login_tickets 的 Mongo 读写(异步, 惰性连接), 按 crawler_id 隔离。"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._client: Any = None
        self._coll: Any = None

    @property
    def _crawler_id(self) -> str:
        return (self._cfg.crawler_id or "default").strip() or "default"

    async def _collection(self) -> Any:
        if self._coll is None:
            self._client = AsyncIOMotorClient(
                self._cfg.mongo_uri, serverSelectionTimeoutMS=3000
            )
            self._coll = self._client[self._cfg.mongo_db][TICKET_COLLECTION]
        return self._coll

    async def get(self, host: str) -> Any:
        doc = await (await self._collection()).find_one(
            {"crawler_id": self._crawler_id, "host": host}
        )
        return (doc or {}).get("ticket")

    async def set(self, ticket: Any, host: str) -> Any:
        if ticket is None:
            raise ValueError("ticket 不能为空, 请传入要存储的登录凭据")
        await (await self._collection()).update_one(
            {"crawler_id": self._crawler_id, "host": host},
            {"$set": {"ticket": ticket, "updated_at": _now_iso()}},
            upsert=True,
        )
        return ticket


class _SaveManager:
    """本地落盘 + 数据 webhook 收集回调。"""

    def __init__(
        self,
        dev_limit: bool,
        max_items: int,
        max_bytes: int,
        output_dir: Path,
        on_saved: Any = None,
    ) -> None:
        self.dev_limit = dev_limit
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.output_dir = output_dir
        self._on_saved = on_saved  # async callable(item)
        self.saved: list[dict[str, Any]] = []

    def limit_items(self, data: Any, n: int | None = None) -> Any:
        limit = n if n is not None else self.max_items
        if not self.dev_limit or not limit or limit <= 0:
            return data
        if isinstance(data, (list, tuple)):
            return data[:limit]
        if isinstance(data, (dict, set, frozenset)):
            return list(data)[:limit]
        if hasattr(data, "__next__"):
            return islice(data, limit)
        return data

    async def save_page(self, page: Any) -> str:
        html = await page.content()
        if self.dev_limit and self.max_bytes > 0:
            html = cap_text_bytes(
                html,
                self.max_bytes,
                notice=f"\n<!-- [已截断: 开发模式限制单次保存不超过 {self.max_bytes} 字节] -->",
            )
        return await self._write("page", ".html", html, html, fmt="html")

    async def save_content(self, data: Any, fmt: str = "txt") -> str:
        fmt = normalize_fmt(fmt)
        max_items = self.max_items if self.dev_limit else None
        max_bytes = self.max_bytes if self.dev_limit else None
        ext, raw, display = prepare_save(data, fmt, max_items=max_items, max_bytes=max_bytes)
        return await self._write(
            "img" if fmt == "img" else "content", ext, raw, display, fmt=fmt
        )

    async def _write(self, kind: str, ext: str, data: Any, display: Any, fmt: str) -> str:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        raw = data.encode("utf-8") if isinstance(data, str) else data
        name = f"{kind}_{uuid.uuid4().hex[:8]}{ext}"
        path = self.output_dir / name
        await asyncio.to_thread(path.write_bytes, raw)
        item = {
            "name": name,
            "kind": kind,
            "fmt": fmt,
            "path": str(path),
            "size": len(raw),
            "content": display if display is not None else (data if isinstance(data, str) else ""),
        }
        self.saved.append(item)
        if self._on_saved is not None:
            try:
                await self._on_saved(item)
            except Exception:  # noqa: BLE001
                pass
        return str(path)


class RunEnv:
    """一次运行的注入环境容器(与平台 CrawlerEnv 的公开函数保持一致)。"""

    def __init__(
        self,
        cfg: Config,
        page: Any,
        context: Any,
        browser: Any,
        saver: _SaveManager,
        tickets: _MongoTicketStore,
        headless: bool,
    ) -> None:
        self.cfg = cfg
        self.page = page
        self.context = context
        self.browser = browser
        self.saver = saver
        self.tickets = tickets
        self.headless = headless

    async def save_page(self) -> str:
        return await self.saver.save_page(self.page)

    async def save_content(self, data: Any, fmt: str = "txt") -> str:
        return await self.saver.save_content(data, fmt)

    def limit_items(self, data: Any, n: int | None = None) -> Any:
        return self.saver.limit_items(data, n)

    async def get_login_ticket(self, host: str) -> Any:
        return await self.tickets.get(host)

    async def set_login_ticket(self, ticket: Any, host: str) -> Any:
        return await self.tickets.set(ticket, host)

    @contextlib.asynccontextmanager
    async def verify_check(self, *args: Any, **kwargs: Any) -> AsyncIterator[_VerifyScope]:
        yield _VerifyScope()

    async def page_login(
        self,
        method: str = "",
        url: str = "",
        account_selector: str = "",
        password_selector: str = "",
        captcha_selector: str = "",
        send_selector: str = "",
        submit_selector: str = "",
        qr_selector: str = "",
        timeout: float = 180,
    ) -> dict:
        """独立运行的交互登录: 打印提示, 在浏览器窗口人工扫码/输入, 轮询跳转。

        - method="qr":      打印提示, 用户扫码; 轮询页面跳转, 跳转即登录完成;
        - method="account"/"sms": 打印提示, 用户在浏览器窗口输入; 轮询跳转。
        不弹任何窗口, 不依赖前端答复。
        """
        method = (method or "").strip()
        if method not in ("qr", "account", "sms"):
            return {
                "ok": False,
                "method": method or "unknown",
                "url": url,
                "error": "page_login 的 method 必须显式指定为 qr/account/sms 之一",
            }
        url = (url or "").strip()
        try:
            current = str(self.page.url)
        except Exception:  # noqa: BLE001
            current = ""
        if url and not _same_page(url, current):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(0.6)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "method": method, "url": url, "error": f"导航到登录页失败: {exc}"}
        try:
            start_url = str(self.page.url)
        except Exception:  # noqa: BLE001
            start_url = url
        print("=" * 64)
        if method == "qr":
            print("【登录】请在浏览器窗口中扫描二维码完成登录。")
        else:
            print("【登录】请在浏览器窗口中输入账号/密码或验证码完成登录。")
        print("登录成功后脚本会自动继续, 无需在此处操作。")
        print(f"当前登录页: {start_url}")
        if self.headless:
            print("提示: 当前为无头模式(headless), 无法看到或操作浏览器窗口;")
            print("      需要人工登录时请使用 --no-headless 重新运行。")
        print("=" * 64)
        ok = await self._wait_for_login(start_url, timeout)
        try:
            final_url = str(self.page.url)
        except Exception:  # noqa: BLE001
            final_url = ""
        if ok:
            print(f"【登录】检测到登录完成: {final_url}")
            return {"ok": True, "method": method, "url": final_url, "error": ""}
        return {
            "ok": False,
            "method": method,
            "url": final_url,
            "error": f"等待登录超时({timeout}s), 未检测到页面跳转",
        }

    async def _wait_for_login(self, start_url: str, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1.0, float(timeout or 180))
        baseline = start_url
        while loop.time() < deadline:
            await asyncio.sleep(1.0)
            try:
                current = str(self.page.url)
            except Exception:  # noqa: BLE001
                current = ""
            if not current:
                continue
            try:
                current_host = bool(urlsplit(current).hostname)
                base_host = bool(urlsplit(baseline).hostname)
            except ValueError:
                continue
            if not current_host:
                continue
            if not base_host:
                baseline = current
                continue
            if not _same_page(baseline, current):
                return True
        return False

    async def capture_login_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"url": "", "cookies": [], "localStorage": {}, "sessionStorage": {}}
        try:
            state["url"] = str(self.page.url)
        except Exception:  # noqa: BLE001
            pass
        try:
            state["cookies"] = await self.context.cookies()
        except Exception:  # noqa: BLE001
            state["cookies"] = []
        for key in ("localStorage", "sessionStorage"):
            try:
                state[key] = await self.page.evaluate(
                    f"Object.fromEntries(Object.entries({key}))"
                )
            except Exception:  # noqa: BLE001
                pass
        return state

    async def restore_login_state(self, state: dict) -> str:
        state = state if isinstance(state, dict) else {}
        cookies = state.get("cookies") or []
        if cookies:
            try:
                await self.context.add_cookies(cookies)
            except Exception:  # noqa: BLE001
                pass
        for store, key in (
            (state.get("localStorage") or {}, "localStorage"),
            (state.get("sessionStorage") or {}, "sessionStorage"),
        ):
            if not store:
                continue
            js = (
                "(() => { const d = " + json.dumps(store, ensure_ascii=False) + ";"
                " for (const k of Object.keys(d)) { try { " + key + ".setItem(k, d[k]); } catch (e) {} } })()"
            )
            try:
                await self.page.add_init_script(js)
                await self.page.evaluate(js)
            except Exception:  # noqa: BLE001
                pass
        count = len(cookies) + len(state.get("localStorage") or {}) + len(
            state.get("sessionStorage") or {}
        )
        return f"已恢复 {count} 项登录态(cookies/localStorage/sessionStorage)"


def resolve_headless(cfg: Config) -> bool:
    """无头模式: 显式指定优先, 否则 run 模式默认无头。"""
    if cfg.headless is not None:
        return bool(cfg.headless)
    return (cfg.mode or "dev") == "run"


def _load_crawler_module(code: str, workspace: Path) -> Any:
    """把用户代码渲染为可 import 的模块(每次新加载, 避免 cron 多次运行串状态)。"""
    path = workspace / "crawler.py"
    path.write_text(render_crawler_module(code), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("crawler_run", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 crawler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Launched:
    """一次启动的浏览器句柄, close() 统一回收。"""

    def __init__(self, pw: Any, browser: Any, context: Any, page: Any) -> None:
        self.pw = pw
        self.browser = browser
        self.context = context
        self.page = page

    async def close(self) -> None:
        try:
            if self.browser is not None:
                await self.browser.close()
            if self.pw is not None:
                await self.pw.stop()
        except Exception:  # noqa: BLE001
            pass


async def _launch(cfg: Config, headless: bool) -> _Launched:
    """启动全新 Playwright 浏览器(带 UA 去头处理)。"""
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context()
    page = await context.new_page()
    # 去掉 UA 中的 Headless 标记, 规避部分站点对无头 UA 的拦截
    try:
        real_ua = (await page.evaluate("navigator.userAgent")).replace(
            "HeadlessChrome", "Chrome"
        )
        if real_ua and "Headless" not in real_ua:
            await context.close()
            context = await browser.new_context(user_agent=real_ua)
            page = await context.new_page()
    except Exception:  # noqa: BLE001
        pass
    return _Launched(pw, browser, context, page)


async def execute(
    cfg: Config,
    code: str,
    on_saved: Any = None,
    tickets: _MongoTicketStore | None = None,
    headless: bool | None = None,
    output_dir: Path | None = None,
    launcher: Any = None,
) -> dict[str, Any]:
    """运行一次: 启动全新浏览器执行代码, 返回 {"ok", "error", "output", "saved"}。

    launcher: 可注入的启动器(async callable(cfg, headless) -> _Launched), 测试用。
    """
    headless = resolve_headless(cfg) if headless is None else bool(headless)
    workspace = output_dir or Path(tempfile.mkdtemp(prefix="acs-run-"))
    mod = _load_crawler_module(code, workspace)
    tickets = tickets or _MongoTicketStore(cfg)
    saver = _SaveManager(
        bool(getattr(cfg, "dev_limit", True)),
        int(getattr(cfg, "max_items", 50)),
        int(getattr(cfg, "max_bytes", 512 * 1024)),
        workspace / "output",
        on_saved=on_saved,
    )
    launched: _Launched | None = None
    try:
        launched = await (launcher or _launch)(cfg, headless)
        page = launched.page
        context = launched.context
        browser = launched.browser
        env = RunEnv(cfg, page, context, browser, saver, tickets, headless)
        await mod.run(
            page=page,
            context=context,
            browser=browser,
            save_page=env.save_page,
            save_content=env.save_content,
            limit_items=env.limit_items,
            get_login_ticket=env.get_login_ticket,
            set_login_ticket=env.set_login_ticket,
            page_login=env.page_login,
            verify_check=env.verify_check,
            VerificationFailed=VerificationFailed,
            capture_login_state=env.capture_login_state,
            restore_login_state=env.restore_login_state,
        )
        return {"ok": True, "error": "", "output": "", "saved": saver.saved}
    except VerificationFailed as exc:
        return {"ok": False, "error": str(exc), "output": "", "saved": saver.saved}
    except Exception:  # noqa: BLE001
        return {
            "ok": False,
            "error": traceback.format_exc(),
            "output": "",
            "saved": saver.saved,
        }
    finally:
        if launched is not None:
            await launched.close()