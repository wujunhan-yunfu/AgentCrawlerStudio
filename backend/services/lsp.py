"""LSP 服务层: 将浏览器 WebSocket 与 pyright-langserver 子进程桥接(全异步)。

架构:
    MonacoLanguageClient (浏览器)  --JSON-RPC/WS-->  /ws/lsp (FastAPI)
        --Content-Length/stdio-->  pyright-langserver (node) 子进程
        --Content-Length/stdio-->  诊断/补全/悬停/签名 结果回到浏览器

要点:
    - 每个 WebSocket 会话启动一个 pyright-langserver 子进程(stdio), 会话结束即退出;
      子进程通过 asyncio.create_subprocess_exec 启动, stdio 读写走 asyncio 流,
      全程无线程、无阻塞。
    - 以 `.venv/bin/python` 作为分析核心: 通过 settings.python.pythonPath 指定,
      pyright 会运行该解释器解析 sys.path, 从而基于 venv 中实际安装的库
      (playwright / httpx / bs4 / lxml / requests 等)做准确的类型推断与补全。
    - 工作区为临时目录, 内含 xvfb_env.py 桩模块, 使 pyright 能解析
      page / context / browser 等注入全局的类型, 不会报未定义。
    - 发送给 pyright 的文档会在顶部注入
      "from xvfb_env import page, context, browser, ...",
      并将诊断行号回退一行, 从而让补全/悬停/类型检查能看到注入对象。
    - 代码格式化不再走语言服务器(pyright 不支持 formatting), 改由前端
      DocumentFormattingEditProvider 调用后端 /api/v1/format(black) 完成。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

INJECTED_GLOBALS = (
    "page",
    "context",
    "browser",
    "save_page",
    "save_content",
    "limit_items",
    "get_login_ticket",
    "set_login_ticket",
    "page_login",
    "capture_login_state",
    "restore_login_state",
)

ENV_STUB = """# page / context / browser
from playwright.async_api import Browser, BrowserContext, Page

page: Page
context: BrowserContext
browser: Browser


async def save_page() -> str:
    \"\"\"保存当前页面的完整 HTML 到 tmp/saved, 返回文件绝对路径\"\"\"
    ...


async def save_content(data: str, fmt: str = "txt") -> str:
    \"\"\"保存数据到 tmp/saved, 返回文件绝对路径; fmt 支持 txt(默认)/json/jsonl/csv/img\"\"\"
    ...


def limit_items(data, n=None):
    \"\"\"开发测试模式限制遍历长度(列表取前 n 条, 迭代器走 islice), 默认取 max_items; 生产模式原样返回\"\"\"
    ...


async def get_login_ticket(host: str):
    \"\"\"从 MongoDB 读取指定 host 下储存的 ticket, 未找到返回 None。

    仅接收 host 参数, 内部不对凭据做任何处理(不注入/不解析/不推导类型),
    直接把该 host 下储存的 ticket 原样返回; ticket 的获取与使用由业务代码自行实现\"\"\"
    ...


async def set_login_ticket(ticket, host: str):
    \"\"\"将 ticket 值直接储存在指定的 host 下(不存在则新建); 返回写入的 ticket。

    仅接收 ticket 和 host 参数, 内部不对凭据做任何处理(不提取/不注入/不编码),
    直接把 ticket 值原样储存; 凭据的注入与使用由业务代码自行实现\"\"\"
    ...


async def page_login(method: str,
                     url: str = "",
                     account_selector: str = "",
                     password_selector: str = "",
                     captcha_selector: str = "",
                     send_selector: str = "",
                     submit_selector: str = "",
                     qr_selector: str = "",
                     timeout: float = 180) -> dict:
    \"\"\"与用户协作完成登录(仅唤起用户登录, 不保存凭据): method 必填, 必须显式指定
    qr/account/sms 之一, 不支持 auto; 二维码放大画面扫码, 账密/验证码弹模拟登录框;
    browser_run_code 每次全新浏览器(初始 about:blank), 当前非登录页时传 url=登录页URL
    会自动先导航到登录页再交互, 交互期间不变更/刷新页面;
    登录成功后如需复用凭据, 由业务代码自行提取并用 set_login_ticket(ticket, host) 保存\"\"\"
    ...


async def capture_login_state() -> dict:
    \"\"\"读取浏览器 Application>Storage 数据(devtools 的 Storage 面板):
    cookies(含 HttpOnly, 多路径兜底采集)/ localStorage / sessionStorage;
    并返回 credentials 字段: 对 cookie 与 storage 中疑似鉴权凭据
    (token/jwt/session/authorization 等)的分类, 供判断真实鉴权凭据来源
    (cookie 或 localStorage/sessionStorage, JWT 站点常在后者)\"\"\"
    ...


async def restore_login_state(state: dict) -> str:
    \"\"\"把登录态快照恢复进当前浏览器(含 cookies/localStorage/sessionStorage),
    新浏览器也可直接拿到登录态\"\"\"
    ...
"""

INJECTED_LINE = (
    "from xvfb_env import page, context, browser, "
    "save_page, save_content, limit_items, get_login_ticket, set_login_ticket, "
    "page_login, capture_login_state, restore_login_state"
)

# 命中这些模式的诊断会被过滤(注入全局的"未定义变量"提示)
# pyright 的诊断文案为 "Undefined name \"page\""(首字母大写), 需忽略大小写匹配。
_UNDEFINED_RE = re.compile(
    r"(?:undefined name ['\"]|Name ['\"])(" + "|".join(INJECTED_GLOBALS) + r")(?:['\"]|\))",
    re.IGNORECASE,
)

# 请求中携带文档位置/范围的 method -> 需要整体行号 +1 的字段路径
_POSITION_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "textDocument/completion": (("position",),),
    "textDocument/hover": (("position",),),
    "textDocument/signatureHelp": (("position",),),
    "textDocument/definition": (("position",),),
    "textDocument/typeDefinition": (("position",),),
    "textDocument/declaration": (("position",),),
    "textDocument/references": (("position",),),
    "textDocument/documentHighlight": (("position",),),
    "textDocument/rename": (("position",),),
    "textDocument/codeAction": (("range",),),
    "callHierarchy/incomingCalls": (("position",),),
    "callHierarchy/outgoingCalls": (("position",),),
}


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _python_path() -> str:
    """返回用于分析的 Python 解释器: 优先项目 venv 的 .venv/bin/python。"""
    venv_python = _project_root() / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _pyright_settings() -> dict[str, Any]:
    return {
        "python": {
            "pythonPath": _python_path(),
            "analysis": {
                "useLibraryCodeForTypes": True,
                "typeCheckingMode": "basic",
                "diagnosticMode": "openFilesOnly",
                "autoImportCompletions": True,
            },
        }
    }


def _langserver_command() -> list[str]:
    """定位 pyright-langserver 可执行文件(前端 npm 依赖)。"""
    root = _project_root()
    candidates = [
        root / "frontend" / "node_modules" / ".bin" / "pyright-langserver",
        root / "node_modules" / ".bin" / "pyright-langserver",
    ]
    for candidate in candidates:
        if candidate.exists():
            return [str(candidate), "--stdio"]
    found = shutil.which("pyright-langserver")
    if found:
        return [found, "--stdio"]
    raise RuntimeError(
        "未找到 pyright-langserver, 请先在 frontend 安装: npm install --prefix frontend"
    )


def _filter_injected_globals(message: str) -> bool:
    """判断诊断消息是否为注入全局(page/context/browser)的未定义提示。"""
    return _UNDEFINED_RE.search(message) is None


async def _read_jsonrpc(stream: asyncio.StreamReader) -> dict | None:
    """异步读取一条 LSP 消息(Content-Length 帧)。流结束返回 None。"""
    headers: dict[bytes, bytes] = {}
    while True:
        line = await stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()
    length_header = headers.get(b"content-length")
    if length_header is None:
        return None
    length = int(length_header)
    if length <= 0:
        return None
    payload = await stream.readexactly(length)
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


async def _write_jsonrpc(stream: asyncio.StreamWriter, message: dict) -> None:
    """异步写入一条 LSP 消息(Content-Length 帧)。"""
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload)
    await stream.drain()


class LspManager:
    """管理 LSP 工作区与每个 WebSocket 会话对应的 pyright 子进程。

    单编辑器场景: 共用一个临时工作区(含 xvfb_env.py 桩模块),
    每次 WebSocket 连接新建一个 LspSession 并为其启动独立的 pyright 进程。
    单一事件循环内所有操作均为原子, 无需加锁。
    """

    def __init__(self) -> None:
        self._workspace: Path | None = None
        self._doc_uri = ""
        self._doc_path: Path | None = None
        self._sessions: list["LspSession"] = []

    def ensure_workspace(self) -> tuple[Path, str, Path]:
        """创建(或复用)临时工作区, 返回 (workspace, doc_uri, doc_path)。"""
        if self._workspace is None:
            base = Path(tempfile.mkdtemp(prefix="xvfb-lsp-"))
            (base / "xvfb_env.py").write_text(ENV_STUB, encoding="utf-8")
            self._workspace = base
            self._doc_path = base / "main.py"
            self._doc_uri = self._doc_path.as_uri()
        return self._workspace, self._doc_uri, self._doc_path  # type: ignore[return-value]

    def info(self) -> dict[str, Any]:
        workspace, doc_uri, _ = self.ensure_workspace()
        return {
            "name": "pyright",
            "workspace": str(workspace),
            "uri": doc_uri,
            "doc_uri": doc_uri,
            "injected_globals": list(INJECTED_GLOBALS),
            "python_path": _python_path(),
        }

    async def create_session(self, ws: Any) -> "LspSession":
        workspace, doc_uri, doc_path = self.ensure_workspace()
        session = await LspSession.create(ws, workspace, doc_uri, doc_path)
        self._sessions.append(session)
        return session

    def drop_session(self, session: "LspSession") -> None:
        if session in self._sessions:
            self._sessions.remove(session)


class LspSession:
    """一次 WebSocket 会话与 pyright 子进程的桥接(全异步, 无线程)。"""

    def __init__(self, ws: Any, workspace: Path, doc_uri: str, doc_path: Path) -> None:
        self._ws = ws
        self._workspace = workspace
        self._doc_uri = doc_uri
        self._doc_path = doc_path
        self._doc_text = ""
        self._settings = _pyright_settings()
        # 已转发给 pyright 的请求 id -> method, 用于按请求类型修正响应坐标
        self._pending: dict[str, str] = {}
        self._proc: Any = None
        self._reader_task: asyncio.Task | None = None

    # ------------------------------------------------------------ 启动/退出

    @classmethod
    async def create(cls, ws: Any, workspace: Path, doc_uri: str, doc_path: Path) -> "LspSession":
        """创建会话并异步启动 pyright 子进程(stdio)。"""
        session = cls(ws, workspace, doc_uri, doc_path)
        session._proc = await asyncio.create_subprocess_exec(
            *_langserver_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=os.environ.copy(),
            cwd=str(workspace),
        )
        return session

    async def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_pyright())

    async def stop(self) -> None:
        """终止 pyright 子进程并清理读取任务(幂等)。"""
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            self._proc = None

    # ------------------------------------------------------------ WS -> pyright

    async def pump(self) -> None:
        """从浏览器 WebSocket 读取 JSON-RPC 消息并转交给 pyright。"""
        try:
            while True:
                text = await self._ws.receive_text()
                try:
                    message = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if isinstance(message, dict):
                    req_id = message.get("id")
                    if req_id is not None and isinstance(message.get("method"), str):
                        self._pending[str(req_id)] = message["method"]
                    await self._patch_request(message)
                    await self._write_message(message)
        except Exception:
            pass
        finally:
            await self.stop()

    # ------------------------------------------------------- pyright -> WS

    async def _read_pyright(self) -> None:
        """从 pyright 子进程 stdout 异步读取消息并转发给浏览器。"""
        stdout = self._proc.stdout
        try:
            while True:
                message = await _read_jsonrpc(stdout)
                if message is None:
                    break
                await self._on_lsp_message(message)
        except Exception:  # noqa: BLE001
            pass

    async def _on_lsp_message(self, message: dict) -> None:
        try:
            method = message.get("method")
            if method in ("workspace/configuration", "window/workDoneProgress/create"):
                # pyright -> 客户端 的请求: 由本桥接直接回包给 pyright(stdio),
                # 不能转发给浏览器(浏览器不会也无法回应, 会导致 pyright 一直等待)。
                response = self._handle_server_request(message)
                if response:
                    await self._write_message(response)
                return
            await self._patch_response(message)
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._ws.send_text(json.dumps(message, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

    async def _write_message(self, message: dict) -> None:
        await _write_jsonrpc(self._proc.stdin, message)

    def _handle_server_request(self, message: dict) -> dict:
        req_id = message.get("id")
        if req_id is None:
            return {}
        method = message.get("method")
        if method == "window/workDoneProgress/create":
            return {"jsonrpc": "2.0", "id": req_id, "result": None}
        sections = (message.get("params") or {}).get("items") or []
        result = []
        for item in sections:
            section = (item or {}).get("section")
            result.append(self._settings.get(section) if isinstance(section, str) else {})
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    # ------------------------------------------------------------ 报文改写

    async def _patch_request(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            params["rootUri"] = self._workspace.as_uri()
            params["rootPath"] = str(self._workspace)
            params.setdefault("workspaceFolders", [])
            params["workspaceFolders"] = [
                {"uri": self._workspace.as_uri(), "name": "workspace"}
            ]
            params["initializationOptions"] = self._settings
            message["params"] = params
        elif method == "textDocument/didOpen":
            text_document = params.get("textDocument") or {}
            text = text_document.get("text", "")
            self._set_doc_text(text)
            text_document["text"] = self._injected_text()
            params["textDocument"] = text_document
            message["params"] = params
            await self._sync_doc_to_disk()
        elif method == "textDocument/didChange":
            text_document = params.get("textDocument") or {}
            changes = params.get("contentChanges") or []
            for change in changes:
                self._apply_change(change)
            params["contentChanges"] = [{"text": self._injected_text()}]
            params["textDocument"] = text_document
            message["params"] = params
            await self._sync_doc_to_disk()
        elif method == "textDocument/didSave":
            await self._sync_doc_to_disk()
        elif method == "workspace/didChangeConfiguration":
            params["settings"] = self._settings
            message["params"] = params
        elif method in _POSITION_PATHS:
            # 文档在 pyright 侧多了一行注入 import, 请求位置行号整体 +1
            for path in _POSITION_PATHS[method]:
                node: Any = params
                for key in path[:-1]:
                    node = node.get(key) or {}
                target = node.get(path[-1])
                if target:
                    # range(如 codeAction)按 start/end 行号整体 +1, position 直接 +1
                    if isinstance(target, dict) and "start" in target:
                        self._shift_range(target, 1)
                    else:
                        self._shift_pos(target, 1)

    async def _patch_response(self, message: dict) -> None:
        method = message.get("method")
        if method == "textDocument/publishDiagnostics":
            self._patch_diagnostics(message)
            return
        rid = message.get("id")
        if rid is None or "result" not in message:
            return
        req_method = self._pending.pop(str(rid), None)
        if req_method is None:
            return
        result = message.get("result")
        try:
            if req_method in ("textDocument/completion", "completionItem/resolve"):
                self._patch_completion(result)
            elif req_method in (
                "textDocument/definition",
                "textDocument/typeDefinition",
                "textDocument/declaration",
                "textDocument/references",
            ):
                self._shift_locations(result, -1)
            elif req_method == "textDocument/hover":
                if isinstance(result, dict):
                    self._shift_range(result.get("range"), -1)
            elif req_method == "textDocument/documentHighlight":
                for item in result or []:
                    self._shift_range(item.get("range"), -1)
            elif req_method == "textDocument/documentSymbol":
                self._shift_symbols(result, -1)
            elif req_method == "textDocument/rename":
                self._shift_workspace_edit(result, -1)
            elif req_method == "textDocument/codeAction":
                for action in result or []:
                    self._shift_workspace_edit(action.get("edit"), -1)
            elif req_method in ("callHierarchy/incomingCalls", "callHierarchy/outgoingCalls"):
                for item in result or []:
                    self._shift_range(item.get("range"), -1)
                    self._shift_range(item.get("selectionRange"), -1)
        except Exception:  # noqa: BLE001
            pass

    def _patch_completion(self, result: Any) -> None:
        if isinstance(result, dict) and "items" in result:
            items = result["items"]
        elif isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = [result]
        else:
            return
        for item in items:
            self._shift_text_edit(item.get("textEdit"), -1)
            for te in item.get("additionalTextEdits") or []:
                self._shift_text_edit(te, -1)

    def _shift_symbols(self, symbols: Any, delta: int) -> None:
        for sym in symbols or []:
            self._shift_range(sym.get("range"), delta)
            self._shift_range(sym.get("selectionRange"), delta)
            self._shift_symbols(sym.get("children"), delta)

    def _shift_locations(self, result: Any, delta: int) -> None:
        if isinstance(result, list):
            for loc in result:
                self._shift_location(loc, delta)
        elif isinstance(result, dict):
            self._shift_location(result, delta)

    def _shift_workspace_edit(self, edit: Any, delta: int) -> None:
        if not isinstance(edit, dict):
            return
        for edits in (edit.get("changes") or {}).values():
            for te in edits:
                self._shift_text_edit(te, delta)
        for dc in edit.get("documentChanges") or []:
            if isinstance(dc, dict) and "textEdits" in dc:
                for te in dc["textEdits"]:
                    self._shift_text_edit(te, delta)
            elif isinstance(dc, dict) and "edits" in dc:
                for te in dc["edits"]:
                    self._shift_text_edit(te, delta)

    def _shift_text_edit(self, te: Any, delta: int) -> None:
        if not isinstance(te, dict):
            return
        self._shift_range(te.get("range"), delta)
        self._shift_range(te.get("insert"), delta)
        self._shift_range(te.get("replace"), delta)

    def _patch_diagnostics(self, message: dict) -> None:
        params = message.get("params") or {}
        diagnostics = params.get("diagnostics") or []
        kept = []
        for d in diagnostics:
            rng = d.get("range") or {}
            start_line = (rng.get("start") or {}).get("line")
            if start_line == 0:
                # 第 0 行是我们注入的 import 行, 只存在于 pyright 文档中
                continue
            if _filter_injected_globals(d.get("message", "")):
                kept.append(d)
        params["diagnostics"] = kept
        for diagnostic in kept:
            self._shift_range(diagnostic.get("range"), -1)
        message["params"] = params

    @staticmethod
    def _shift_pos(pos: dict | None, delta: int) -> None:
        if isinstance(pos, dict) and isinstance(pos.get("line"), int):
            pos["line"] = max(0, pos["line"] + delta)

    def _shift_range(self, rng: dict | None, delta: int) -> None:
        if not rng:
            return
        self._shift_pos(rng.get("start"), delta)
        self._shift_pos(rng.get("end"), delta)

    def _shift_location(self, loc: dict | None, delta: int) -> None:
        if isinstance(loc, dict):
            self._shift_range(loc.get("range"), delta)

    # ---------------------------------------------------------- 文档维护

    def _injected_text(self) -> str:
        return INJECTED_LINE + "\n" + self._doc_text

    def _set_doc_text(self, text: str) -> None:
        self._doc_text = text

    def _apply_change(self, change: dict) -> None:
        text = change.get("text", "")
        rng = change.get("range")
        if not rng:
            self._doc_text = text
            return
        lines = self._doc_text.split("\n")
        last = len(lines) - 1
        start_line = max(0, min(rng["start"]["line"], last))
        start_col = min(rng["start"]["character"], len(lines[start_line]))
        end_line = max(0, min(rng["end"]["line"], last))
        end_col = min(rng["end"]["character"], len(lines[end_line]))
        if start_line == end_line:
            line = lines[start_line]
            lines[start_line] = line[:start_col] + text + line[end_col:]
        else:
            merged = lines[start_line][:start_col] + text + lines[end_line][end_col:]
            lines = lines[:start_line] + [merged] + lines[end_line + 1 :]
        self._doc_text = "\n".join(lines)

    async def _sync_doc_to_disk(self) -> None:
        text = self._injected_text()
        try:
            await asyncio.to_thread(self._doc_path.write_text, text, "utf-8")
        except OSError:
            pass
