"""导出脚本包: 把编辑器中的代码打包为可直接用 uv 运行的独立工程。

生成 zip 包结构:
    <name>/
        pyproject.toml  uv 工程定义(依赖 playwright + 脚本检测到的第三方库)
        README.md       运行说明(立即运行 / 定时运行 / cron 校验)
        main.py         入口: 立即运行或按 cron 定时运行
        runtime.py      Playwright 运行环境与注入函数
        crawler.py      用户脚本(包成 async def run(...), 直接 import 运行)
        cron.py         cron 表达式校验(纯标准库)
        .gitignore

导出时静态扫描用户脚本的 import, 把检测到的第三方依赖追加进 pyproject.toml。
导出的脚本直接使用 Playwright 自带的无头/有头浏览器, 不依赖 Xvfb;
--headless 的默认值由导出时选择(嵌入 DEFAULT_HEADLESS)。
cron.py 直接复用 backend.services.cron 的源码, 保证后端校验与包内校验一致。
"""

from __future__ import annotations

import ast
import io
import re
import sys
import zipfile
from pathlib import Path

from .cron import validate_cron

_CRON_SOURCE = Path(__file__).with_name("cron.py").read_text(encoding="utf-8")

_NAME_RE = re.compile(r"[^0-9A-Za-z_\-]+")

# 导入名 -> PyPI 分发包名(常见不一致项)
_DIST_ALIASES = {
    "bs4": "beautifulsoup4",
    "PIL": "pillow",
    "yaml": "PyYAML",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "OpenSSL": "pyOpenSSL",
    "jwt": "PyJWT",
    "serial": "pyserial",
    "fake_useragent": "fake-useragent",
    "xlsxwriter": "XlsxWriter",
    "lxml": "lxml",
}

# 运行环境已提供 / 导出包自带, 无需写入依赖
_PROVIDED_MODULES = {"playwright", "crawler", "runtime", "cron", "main"}

# run() 的参数(注入给用户脚本的对象/函数)
_INJECTED_PARAMS = (
    "page",
    "context",
    "browser",
    "save_page",
    "save_content",
    "limit_items",
    "get_login_ticket",
    "set_login_ticket",
    "page_login",
    "verify_check",
    "VerificationFailed",
    "capture_login_state",
    "restore_login_state",
)


def safe_name(name: str) -> str:
    """把用户输入的包名规范化为安全的目录/文件名。"""
    cleaned = _NAME_RE.sub("_", (name or "").strip()).strip("_")
    return cleaned or "crawler"


def detect_dependencies(code: str) -> list[str]:
    """静态扫描脚本中的 import, 返回需要追加到 pyproject.toml 的第三方依赖。

    - 跳过标准库、相对导入与导出包自带的模块(crawler/runtime/cron/playwright);
    - 常见导入名按 PyPI 分发包名归一化(如 bs4 -> beautifulsoup4)。
    """
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    deps: set[str] = set()
    for root in roots:
        if not root or root in _PROVIDED_MODULES or root in stdlib:
            continue
        deps.add(_DIST_ALIASES.get(root, root))
    return sorted(deps)


def render_crawler_module(code: str) -> str:
    """把编辑器脚本渲染为可直接 import 的 crawler.py。

    用户代码(含顶层 await)被包进 `async def run(page, context, ...)`,
    注入对象作为 run() 的参数; `from __future__` 导入会被提到文件顶部。
    """
    future: list[str] = []
    body: list[str] = []
    for line in (code or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("from __future__ import"):
            if stripped not in future:
                future.append(stripped)
        else:
            body.append(line)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    indented = "\n".join(("    " + line if line.strip() else "") for line in body)
    if not indented.strip():
        indented = "    pass"
    params = "".join(f"    {name},\n" for name in _INJECTED_PARAMS)
    header = ("\n".join(future) + "\n\n") if future else ""
    return (
        header
        + "async def run(\n"
        + params
        + "):\n"
        + indented
        + "\n"
    )


RUNTIME_PY = '''"""独立运行环境: 启动 Playwright 并执行 crawler.py。

由 AgentCrawlerStudio 导出, 直接使用 Playwright 自带的无头/有头浏览器,
不依赖 Xvfb; page_login 仅打印提示, 请用户在浏览器窗口中扫码/登录, 不弹窗。

向脚本注入:
- page / context / browser: Playwright 异步对象
- save_page() / save_content(data, fmt) / limit_items(data, n)
- get_login_ticket(host) / set_login_ticket(ticket, host)  (本地 JSON 存储)
- page_login(method, ...)  在浏览器窗口人工登录, 打印提示并等待跳转
- capture_login_state() / restore_login_state(state)
- verify_check(...) / VerificationFailed  (独立运行不启用自动过验, 仅占位)
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import re
import traceback
import uuid
from contextlib import asynccontextmanager
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
TICKETS_PATH = BASE_DIR / "login_tickets.json"

_IMAGE_MIME_EXTS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/svg+xml": ".svg", "image/x-icon": ".ico", "image/avif": ".avif",
    "image/tiff": ".tiff", "image/heic": ".heic", "image/heif": ".heif",
}
_DATA_URI_RE = re.compile(r"^data:([^;,]+)(?:;[^,]*)?,(.*)$", re.S)


class VerificationFailed(Exception):
    """人机验证限次未通过(独立运行模式下不会主动抛出)。"""


class _VerifyScope:
    def passed(self) -> bool:
        return True

    def status(self) -> dict:
        return {"enabled": False}


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _same_page(a: str, b: str) -> bool:
    """判断两个 URL 是否指向同一页面(scheme+host+path)。"""
    try:
        ua, ub = urlsplit(a or ""), urlsplit(b or "")
    except ValueError:
        return False
    if not ua.hostname or not ub.hostname:
        return False
    return (ua.scheme, ua.hostname, ua.path) == (ub.scheme, ub.hostname, ub.path)


def _decode_image(data: Any) -> tuple[bytes, str]:
    text = (data if isinstance(data, str) else str(data)).strip()
    mime = "image/png"
    payload = text
    match = _DATA_URI_RE.match(text)
    if match:
        mime = match.group(1).strip().lower()
        payload = match.group(2)
    ext = _IMAGE_MIME_EXTS.get(mime)
    if ext is None:
        ext = f".{mime.split('/')[-1]}" if mime.startswith("image/") else ".png"
    return base64.b64decode(payload), ext


def _to_csv(data: Any) -> str:
    buf = io.StringIO()
    if isinstance(data, list) and data and isinstance(data[0], dict):
        keys = list(data[0].keys())
        writer = csv.DictWriter(buf, fieldnames=keys)
        writer.writeheader()
        for row in data:
            writer.writerow(row if isinstance(row, dict) else {k: "" for k in keys})
    elif isinstance(data, (list, tuple)):
        writer = csv.writer(buf)
        for row in data:
            writer.writerow(row if isinstance(row, (list, tuple)) else [row])
    else:
        raise ValueError("csv 格式需要传入 list[dict] 或 list[list]")
    return buf.getvalue()


class _TicketStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    async def get(self, host: str) -> Any:
        return self._load().get(host)

    async def set(self, ticket: Any, host: str) -> Any:
        data = self._load()
        data[host] = ticket
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return ticket


class _SaveManager:
    def __init__(self, dev_limit: bool, max_items: int, max_bytes: int) -> None:
        self.dev_limit = dev_limit
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.saved: list[dict] = []

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
            html = _cap(html, self.max_bytes)
        return self._write("page", ".html", html, html)

    async def save_content(self, data: Any, fmt: str = "txt") -> str:
        fmt = (fmt or "txt").lower().lstrip(".")
        if fmt == "img":
            raw, ext = _decode_image(data)
            self._mkdir()
            name = f"img_{_new_id()}{ext}"
            path = OUTPUT_DIR / name
            path.write_bytes(raw)
            self.saved.append({"name": name, "path": str(path), "size": len(raw)})
            return str(path)
        if self.dev_limit and isinstance(data, (list, tuple)):
            data = data[: self.max_items]
        if fmt == "json":
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
            ext = ".json"
        elif fmt == "jsonl":
            items = data if isinstance(data, list) else [data]
            text = "\\n".join(
                x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in items
            )
            ext = ".jsonl"
        elif fmt == "csv":
            text, ext = _to_csv(data), ".csv"
        else:
            text, ext = (data if isinstance(data, str) else str(data)), ".txt"
        if self.dev_limit and self.max_bytes > 0:
            text = _cap(text, self.max_bytes)
        return self._write("content", ext, text, text)

    def _mkdir(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def _write(self, kind: str, ext: str, data: str, display: str) -> str:
        self._mkdir()
        name = f"{kind}_{_new_id()}{ext}"
        path = OUTPUT_DIR / name
        path.write_text(data, encoding="utf-8")
        self.saved.append(
            {
                "name": name,
                "path": str(path),
                "size": len(data.encode("utf-8")),
                "content": display,
            }
        )
        return str(path)


def _cap(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    while raw:
        try:
            return raw[:max_bytes].decode("utf-8")
        except UnicodeDecodeError:
            max_bytes -= 1
    return ""


class RuntimeEnv:
    """一次运行的注入环境容器(与平台 CrawlerEnv 的公开函数保持一致)。"""

    def __init__(
        self,
        page: Any,
        context: Any,
        browser: Any,
        dev_limit: bool = False,
        max_items: int = 50,
        max_bytes: int = 512 * 1024,
        headless: bool = False,
    ) -> None:
        self.page = page
        self.context = context
        self.browser = browser
        self.headless = headless
        self.saver = _SaveManager(dev_limit, max_items, max_bytes)
        self.tickets = _TicketStore(TICKETS_PATH)

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

    @asynccontextmanager
    async def verify_check(self, *args: Any, **kwargs: Any):
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
        """独立运行的交互登录: 在浏览器窗口中人工完成登录, 脚本等待其跳转。

        - method="qr":      打印提示, 请用户在浏览器窗口扫描二维码;
        - method="account" / "sms": 打印提示, 请用户在浏览器窗口输入账号/验证码。
        本函数不弹任何窗口, 仅打印信息并轮询页面跳转, 检测到登录完成后自动返回。
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
        except Exception:
            current = ""
        if url and not _same_page(url, current):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(0.6)
            except Exception as exc:
                return {"ok": False, "method": method, "url": url, "error": f"导航到登录页失败: {exc}"}
        try:
            start_url = str(self.page.url)
        except Exception:
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
        except Exception:
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
        """轮询页面跳转: scheme+host+path 变化即视为登录完成。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1.0, float(timeout or 180))
        baseline = start_url
        while loop.time() < deadline:
            await asyncio.sleep(1.0)
            try:
                current = str(self.page.url)
            except Exception:
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

    async def capture_login_state(self) -> dict:
        state: dict[str, Any] = {"url": "", "cookies": [], "localStorage": {}, "sessionStorage": {}}
        try:
            state["url"] = str(self.page.url)
        except Exception:
            pass
        try:
            state["cookies"] = await self.context.cookies()
        except Exception:
            state["cookies"] = []
        for key in ("localStorage", "sessionStorage"):
            try:
                state[key] = await self.page.evaluate(
                    f"Object.fromEntries(Object.entries({key}))"
                )
            except Exception:
                pass
        return state

    async def restore_login_state(self, state: dict) -> str:
        state = state if isinstance(state, dict) else {}
        cookies = state.get("cookies") or []
        if cookies:
            try:
                await self.context.add_cookies(cookies)
            except Exception:
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
            except Exception:
                pass
        count = len(cookies) + len(state.get("localStorage") or {}) + len(
            state.get("sessionStorage") or {}
        )
        return f"已恢复 {count} 项登录态(cookies/localStorage/sessionStorage)"


async def run_script(
    *,
    headless: bool = False,
    dev_limit: bool = False,
    max_items: int = 50,
    max_bytes: int = 512 * 1024,
) -> dict:
    """启动全新浏览器并运行 crawler.py 中的 run(), 返回 {"ok", "error", "saved"}。"""
    import crawler
    from playwright.async_api import async_playwright

    pw = None
    browser = None
    env_obj: RuntimeEnv | None = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context()
        page = await context.new_page()
        # 部分站点(如大商所)有动态防护, 会检测自动化标识(navigator.webdriver)与
        # 无头 UA(HeadlessChrome)并返回空响应。启动参数已禁用 AutomationControlled,
        # 这里再把 UA 中的 Headless 标记去掉, 使其与普通浏览器一致。
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
        env_obj = RuntimeEnv(
            page, context, browser, dev_limit, max_items, max_bytes, headless=headless
        )
        await crawler.run(
            page=page,
            context=context,
            browser=browser,
            save_page=env_obj.save_page,
            save_content=env_obj.save_content,
            limit_items=env_obj.limit_items,
            get_login_ticket=env_obj.get_login_ticket,
            set_login_ticket=env_obj.set_login_ticket,
            page_login=env_obj.page_login,
            verify_check=env_obj.verify_check,
            VerificationFailed=VerificationFailed,
            capture_login_state=env_obj.capture_login_state,
            restore_login_state=env_obj.restore_login_state,
        )
        return {"ok": True, "error": "", "saved": env_obj.saver.saved}
    except VerificationFailed as exc:
        return {
            "ok": False,
            "error": str(exc),
            "saved": env_obj.saver.saved if env_obj else [],
        }
    except Exception:
        return {
            "ok": False,
            "error": traceback.format_exc(),
            "saved": env_obj.saver.saved if env_obj else [],
        }
    finally:
        try:
            if browser is not None:
                await browser.close()
            if pw is not None:
                await pw.stop()
        except Exception:
            pass
'''


MAIN_PY = '''"""独立爬虫入口: 立即运行 / 定时运行(cron)。

用法:
    uv run python main.py                     立即运行一次
    uv run python main.py --cron "*/5 * * * *"  按 cron 定时运行
    uv run python main.py --cron              使用导出时配置的默认表达式(未配置则报错)
    uv run python main.py --validate-cron "0 8 * * *"  仅校验并查看下次运行时间
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from cron import CronError, next_runs, validate_cron
from runtime import run_script

# 导出时配置的默认 cron 表达式(为空则定时运行必须用 --cron 提供)
DEFAULT_CRON = "__DEFAULT_CRON__"

# 导出时配置的默认运行模式: True=无头, False=有头(可人工扫码/登录)
DEFAULT_HEADLESS = __DEFAULT_HEADLESS__


def _fmt(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


async def run_once(args: argparse.Namespace) -> int:
    print(f"[{_fmt(datetime.now(timezone.utc))}] 开始运行 ...")
    result = await run_script(
        headless=args.headless,
        dev_limit=args.dev_limit,
        max_items=args.max_items,
        max_bytes=args.max_bytes,
    )
    for item in result.get("saved", []):
        print(f"  已保存: {item['path']} ({item['size']} 字节)")
    if result.get("error"):
        print(result["error"], file=sys.stderr)
    print(f"[{_fmt(datetime.now(timezone.utc))}] 运行{'成功' if result.get('ok') else '失败'}")
    return 0 if result.get("ok") else 1


def resolve_cron(args: argparse.Namespace) -> str:
    # 优先使用命令行提供的表达式; 未提供时回退导出时配置的默认值
    expr = (args.cron or "").strip() or DEFAULT_CRON.strip()
    if not expr:
        print(
            "未提供 cron 表达式, 且导出时未配置默认值; "
            '请使用 --cron "分 时 日 月 周" 指定',
            file=sys.stderr,
        )
        raise SystemExit(2)
    ok, error = validate_cron(expr)
    if not ok:
        print(f"cron 表达式无效: {error}", file=sys.stderr)
        raise SystemExit(2)
    print(f"cron 表达式有效: {expr}")
    return expr


async def run_scheduled(expr: str, args: argparse.Namespace) -> int:
    print(f"已启动定时运行, cron: {expr} (Ctrl+C 停止)")
    while True:
        now = datetime.now(timezone.utc)
        upcoming = next_runs(expr, count=1, base=now)[0]
        wait = max(0.0, (upcoming - now).total_seconds())
        print(f"下次运行: {_fmt(upcoming)} (等待 {wait:.0f}s)")
        await asyncio.sleep(wait)
        await run_once(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentCrawlerStudio 导出的独立爬虫脚本")
    parser.add_argument(
        "--cron", nargs="?", const="", default=None,
        help="定时运行; 可跟 cron 表达式, 不带值时使用导出时配置的默认表达式",
    )
    parser.add_argument(
        "--validate-cron", metavar="EXPR",
        help="仅校验 cron 表达式并打印接下来 5 次运行时间",
    )
    parser.add_argument(
        "--headless", action=argparse.BooleanOptionalAction, default=DEFAULT_HEADLESS,
        help=f"无头模式运行浏览器(默认 {'开启' if DEFAULT_HEADLESS else '关闭'}; 用 --no-headless 关闭)",
    )
    parser.add_argument("--dev-limit", action="store_true", help="启用开发测试限制(截断数据量)")
    parser.add_argument("--max-items", type=int, default=50, help="开发限制单次保存条数")
    parser.add_argument("--max-bytes", type=int, default=512 * 1024, help="开发限制单次保存字节数")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.validate_cron is not None:
        ok, error = validate_cron(args.validate_cron)
        if not ok:
            print(f"cron 表达式无效: {error}", file=sys.stderr)
            return 2
        try:
            runs = next_runs(args.validate_cron, count=5)
        except CronError as exc:
            print(f"cron 表达式无效: {exc}", file=sys.stderr)
            return 2
        print("cron 表达式有效, 接下来 5 次运行时间:")
        for moment in runs:
            print("  " + _fmt(moment))
        return 0
    try:
        if args.cron is None:
            return asyncio.run(run_once(args))
        return asyncio.run(run_scheduled(resolve_cron(args), args))
    except KeyboardInterrupt:
        print("\\n已停止")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


PYPROJECT_TOML = '''[project]
name = "__NAME__"
version = "0.1.0"
description = "由 AgentCrawlerStudio 导出的独立爬虫脚本"
requires-python = ">=3.10"
dependencies = [
__DEPENDENCIES__
]
'''


README_MD = '''# __NAME__

由 AgentCrawlerStudio 导出的独立爬虫脚本包, 可直接用 [uv](https://docs.astral.sh/uv/) 运行。

本包直接使用 Playwright 自带的无头 / 有头浏览器, **不依赖 Xvfb**。

## 一、环境准备

```bash
uv sync
uv run playwright install chromium
```

## 二、立即运行(执行一次)

```bash
uv run python main.py
```

默认运行模式(无头 / 有头)由导出时配置, 当前默认: __DEFAULT_HEADLESS_DISPLAY__。
可用 `--headless` / `--no-headless` 在运行时覆盖:

```bash
uv run python main.py --no-headless    # 有头模式(可见浏览器窗口, 便于人工扫码/登录)
uv run python main.py --headless       # 无头模式
```

## 三、定时运行(cron)

执行脚本时用参数选择立即运行或定时运行: 不带参数为立即运行一次, 带 `--cron` 为定时运行。

直接传入 cron 表达式(运行前会先校验):

```bash
uv run python main.py --cron "*/5 * * * *"
```

导出时配置的默认 cron: __DEFAULT_CRON_DISPLAY__

若导出时配置了默认表达式, 也可省略表达式直接使用默认值:

```bash
uv run python main.py --cron
```

若导出时未配置默认值, 则定时运行必须提供表达式, 否则报错退出。

仅校验 cron 表达式并查看接下来 5 次运行时间(不会真正运行脚本):

```bash
uv run python main.py --validate-cron "0 8 * * *"
```

### cron 表达式格式

`分 时 日 月 周`, 例如 `0 8 * * 1-5` 表示每个工作日 08:00。

- 支持 `*`(任意)、`,`(列表)、`-`(范围)、`/`(步进), 如 `*/15 9-18 * * MON-FRI`
- 月份支持 `JAN`-`DEC`, 星期支持 `SUN`-`SAT`(0 与 7 均表示周日)
- 支持宏: `@yearly` `@annually` `@monthly` `@weekly` `@daily` `@midnight` `@hourly`

## 四、其他参数

| 参数 | 说明 |
| --- | --- |
| `--headless` / `--no-headless` | 无头 / 有头模式运行浏览器(默认见上文) |
| `--dev-limit` | 启用开发测试限制(截断保存数据量) |
| `--max-items N` | 开发限制下单次保存的最大条数 |
| `--max-bytes N` | 开发限制下单次保存的最大字节数 |

## 五、脚本可用对象

脚本已被导出为 `crawler.py` 中可直接导入的 `async def run(page, context, ...)` 函数,
由 `main.py` 导入调用(不再以字符串 exec 运行)。脚本内可直接使用以下对象/函数(无需 import):

- `page` / `context` / `browser`: Playwright 异步对象
- `save_page()`: 保存当前页面 HTML 到 `output/`
- `save_content(data, fmt)`: 保存文本 / JSON / JSONL / CSV / base64 图片到 `output/`
- `limit_items(data, n)`: 限制列表/迭代器长度(需配合 `--dev-limit`)
- `page_login(method, url, ..., timeout)`: 交互登录。`method` 为 `qr` / `account` / `sms`；
  仅在终端**打印提示**, 请用户在浏览器窗口中扫码 / 输入账号完成登录, **不弹任何窗口**,
  脚本轮询页面跳转, 检测到登录完成后自动继续
- `get_login_ticket(host)` / `set_login_ticket(ticket, host)`: 本地 `login_tickets.json` 读写
- `capture_login_state()` / `restore_login_state(state)`: 登录态快照与恢复
- `verify_check(...)` / `VerificationFailed`: 占位实现(独立运行不启用自动过验)

保存的文件输出到 `output/` 目录。

> 脚本 `import` 的第三方库会在导出时自动写入 `pyproject.toml` 的 dependencies。
> 需要人工扫码 / 登录时请使用有头模式(`--no-headless`), 否则浏览器窗口不可见。
'''


def build_package(
    code: str,
    name: str = "",
    cron: str = "",
    headless: bool = False,
) -> tuple[str, bytes]:
    """构建导出 zip 包, 返回 (文件名, zip 字节)。cron 非法时抛 ValueError。"""
    cron = (cron or "").strip()
    if cron:
        ok, error = validate_cron(cron)
        if not ok:
            raise ValueError(f"cron 表达式无效: {error}")
    pkg = safe_name(name)
    headless_literal = "True" if headless else "False"
    headless_display = "无头模式(headless)" if headless else "有头模式(可见窗口)"
    main_src = MAIN_PY.replace("__DEFAULT_CRON__", cron).replace(
        "__DEFAULT_HEADLESS__", headless_literal
    )
    readme = (
        README_MD.replace("__NAME__", pkg)
        .replace("__DEFAULT_CRON_DISPLAY__", cron or "(未设置, 定时运行必须用 --cron 提供)")
        .replace("__DEFAULT_HEADLESS_DISPLAY__", headless_display)
    )
    dependencies = ["playwright>=1.40", *detect_dependencies(code)]
    dependency_lines = "\n".join(f'    "{dep}",' for dep in dependencies)
    project = PYPROJECT_TOML.replace("__NAME__", pkg).replace(
        "__DEPENDENCIES__", dependency_lines
    )
    crawler_src = render_crawler_module(code)
    gitignore = "output/\nlogin_tickets.json\n.venv/\n__pycache__/\n*.pyc\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{pkg}/pyproject.toml", project)
        zf.writestr(f"{pkg}/README.md", readme)
        zf.writestr(f"{pkg}/main.py", main_src)
        zf.writestr(f"{pkg}/runtime.py", RUNTIME_PY)
        zf.writestr(f"{pkg}/cron.py", _CRON_SOURCE)
        zf.writestr(f"{pkg}/crawler.py", crawler_src)
        zf.writestr(f"{pkg}/.gitignore", gitignore)
    return f"{pkg}.zip", buf.getvalue()
