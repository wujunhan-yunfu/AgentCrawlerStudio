"""导出脚本包: 把编辑器中的代码打包为可直接用 uv 运行的独立工程。

生成 zip 包结构:
    <name>/
        pyproject.toml  uv 工程定义(依赖 playwright)
        README.md       运行说明(立即运行 / 定时运行 / cron 校验)
        main.py         入口: 立即运行或按 cron 定时运行
        runtime.py      Playwright 运行环境与注入函数
        crawler.py      用户在编辑器中编写的脚本(原样)
        cron.py         cron 表达式校验(纯标准库)
        .gitignore

cron.py 直接复用 backend.services.cron 的源码, 保证后端校验与包内校验一致。
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from .cron import validate_cron

_CRON_SOURCE = Path(__file__).with_name("cron.py").read_text(encoding="utf-8")

_NAME_RE = re.compile(r"[^0-9A-Za-z_\-]+")


def safe_name(name: str) -> str:
    """把用户输入的包名规范化为安全的目录/文件名。"""
    cleaned = _NAME_RE.sub("_", (name or "").strip()).strip("_")
    return cleaned or "crawler"


RUNTIME_PY = '''"""独立运行环境: 启动 Playwright 并执行 crawler.py。

由 AgentCrawlerStudio 导出, 对应平台内的代码执行环境, 向脚本注入:
- page / context / browser: Playwright 异步对象
- save_page() / save_content(data, fmt) / limit_items(data, n)
- get_login_ticket(host) / set_login_ticket(ticket, host)  (本地 JSON 存储)
- capture_login_state() / restore_login_state(state)
- verify_check(...) / VerificationFailed  (独立运行不启用自动过验, 仅占位)
"""

from __future__ import annotations

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
    ) -> None:
        self.page = page
        self.context = context
        self.browser = browser
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

    async def page_login(self, *args: Any, **kwargs: Any) -> dict:
        return {
            "ok": False,
            "error": "独立运行不支持交互式 page_login, 请在 AgentCrawlerStudio 平台内运行该脚本",
        }

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


async def _exec_async(code: str, env: dict) -> None:
    """把脚本整体缩进包进 async 函数, 支持顶层 await。"""
    lines = code.splitlines()
    if not any(line.strip() for line in lines):
        return
    indented = "async def __run__():\\n" + "\\n".join(
        ("    " + line if line.strip() else line) for line in lines
    )
    namespace: dict = {}
    exec(compile(indented, "<crawler-code>", "exec"), env, namespace)
    await namespace["__run__"]()


async def run_script(
    code: str,
    *,
    headless: bool = False,
    dev_limit: bool = False,
    max_items: int = 50,
    max_bytes: int = 512 * 1024,
) -> dict:
    """启动全新浏览器执行脚本, 返回 {"ok", "error", "saved"}。"""
    from playwright.async_api import async_playwright

    pw = None
    browser = None
    env_obj: RuntimeEnv | None = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        env_obj = RuntimeEnv(page, context, browser, dev_limit, max_items, max_bytes)
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
            "verify_check": env_obj.verify_check,
            "VerificationFailed": VerificationFailed,
            "capture_login_state": env_obj.capture_login_state,
            "restore_login_state": env_obj.restore_login_state,
            "__name__": "__main__",
        }
        await _exec_async(code, env)
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
from pathlib import Path

from cron import CronError, next_runs, validate_cron
from runtime import run_script

BASE_DIR = Path(__file__).resolve().parent
CODE_PATH = BASE_DIR / "crawler.py"

# 导出时配置的默认 cron 表达式(为空则运行时交互输入)
DEFAULT_CRON = "__DEFAULT_CRON__"


def load_code() -> str:
    return CODE_PATH.read_text(encoding="utf-8")


def _fmt(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


async def run_once(args: argparse.Namespace) -> int:
    print(f"[{_fmt(datetime.now(timezone.utc))}] 开始运行 ...")
    result = await run_script(
        load_code(),
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
        help="定时运行; 可跟 cron 表达式, 不带值时交互输入并校验",
    )
    parser.add_argument(
        "--validate-cron", metavar="EXPR",
        help="仅校验 cron 表达式并打印接下来 5 次运行时间",
    )
    parser.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
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
    "playwright>=1.40",
]
'''


README_MD = '''# __NAME__

由 AgentCrawlerStudio 导出的独立爬虫脚本包, 可直接用 [uv](https://docs.astral.sh/uv/) 运行。

## 一、环境准备

```bash
uv sync
uv run playwright install chromium
```

## 二、立即运行(执行一次)

```bash
uv run python main.py
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
| `--headless` | 无头模式运行浏览器 |
| `--dev-limit` | 启用开发测试限制(截断保存数据量) |
| `--max-items N` | 开发限制下单次保存的最大条数 |
| `--max-bytes N` | 开发限制下单次保存的最大字节数 |

## 五、脚本可用对象

脚本(`crawler.py`)中可直接使用以下对象/函数(无需 import):

- `page` / `context` / `browser`: Playwright 异步对象
- `save_page()`: 保存当前页面 HTML 到 `output/`
- `save_content(data, fmt)`: 保存文本 / JSON / JSONL / CSV / base64 图片到 `output/`
- `limit_items(data, n)`: 限制列表/迭代器长度(需配合 `--dev-limit`)
- `get_login_ticket(host)` / `set_login_ticket(ticket, host)`: 本地 `login_tickets.json` 读写
- `capture_login_state()` / `restore_login_state(state)`: 登录态快照与恢复
- `verify_check(...)` / `VerificationFailed`: 占位实现(独立运行不启用自动过验)

保存的文件输出到 `output/` 目录。
'''


def build_package(
    code: str,
    name: str = "",
    cron: str = "",
) -> tuple[str, bytes]:
    """构建导出 zip 包, 返回 (文件名, zip 字节)。cron 非法时抛 ValueError。"""
    cron = (cron or "").strip()
    if cron:
        ok, error = validate_cron(cron)
        if not ok:
            raise ValueError(f"cron 表达式无效: {error}")
    pkg = safe_name(name)
    runtime_src = RUNTIME_PY
    main_src = MAIN_PY.replace("__DEFAULT_CRON__", cron)
    readme = README_MD.replace("__NAME__", pkg).replace(
        "__DEFAULT_CRON_DISPLAY__", cron or "(未设置, 运行时交互输入)"
    )
    project = PYPROJECT_TOML.replace("__NAME__", pkg)
    gitignore = "output/\nlogin_tickets.json\n.venv/\n__pycache__/\n*.pyc\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{pkg}/pyproject.toml", project)
        zf.writestr(f"{pkg}/README.md", readme)
        zf.writestr(f"{pkg}/main.py", main_src)
        zf.writestr(f"{pkg}/runtime.py", runtime_src)
        zf.writestr(f"{pkg}/cron.py", _CRON_SOURCE)
        zf.writestr(f"{pkg}/crawler.py", code if code.endswith("\n") else code + "\n")
        zf.writestr(f"{pkg}/.gitignore", gitignore)
    return f"{pkg}.zip", buf.getvalue()
