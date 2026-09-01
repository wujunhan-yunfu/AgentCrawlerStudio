"""爬虫编码器默认可用函数: 保存页面/内容, 登录凭据(MongoDB)存取(全异步)。

默认注入到代码执行环境中的全局函数(async 风格, 调用时需 await):
    save_page()                保存当前页面的完整 HTML 到 tmp/saved
    save_content(data, fmt)    保存数据(文本/JSON/JSONL/CSV/base64 图片)到 tmp/saved
    get_login_ticket(host)     从 MongoDB 读取指定 host 下储存的 ticket(仅读取, 不做任何处理)
    set_login_ticket(ticket, host)  将 ticket 值直接储存在指定的 host 下(不做任何处理)
    limit_items(data, n)       开发测试模式限制遍历/保存长度(列表取前 n 条,
                               迭代器走 islice), 生产模式(--no-dev-limit)原样返回

开发测试模式(dev_limit, 默认开启)会自动限制数据量防止运行过长/token 过多:
- save_content 对列表/元组截断为前 max_items 条, txt 再按 max_bytes 截断;
- save_page 的 HTML 按 max_bytes 截断;
- 同步上线时加 --no-dev-limit(或 DEV_LIMIT=0) 取消所有限制。

saved 内容每次运行代码前会被清空, 并通过运行结果返回给前端展示。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Iterator
from urllib.parse import urlsplit

from ..config import Config, PROJECT_ROOT
from .save import cap_text_bytes, normalize_fmt, prepare_save

SAVED_DIR = PROJECT_ROOT / "tmp" / "saved"

TICKET_COLLECTION = "login_tickets"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _same_page(a: str, b: str) -> bool:
    """判断两个 URL 是否指向同一页面(scheme+host+path), about:blank 视为不同。"""
    try:
        ua, ub = urlsplit(a or ""), urlsplit(b or "")
    except ValueError:
        return False
    if not ua.hostname or not ub.hostname:
        return False
    return (ua.scheme, ua.hostname, ua.path) == (ub.scheme, ub.hostname, ub.path)


# ---------------------------------------------------------- Cookie 探测与归一化

_COOKIE_FIELDS = (
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
)


def _cookie_key(c: dict[str, Any]) -> tuple[str, str, str]:
    # 域名匹配忽略前导点(.example.com 与 example.com 视为同一 cookie)
    domain = str(c.get("domain") or "").lstrip(".")
    return (domain, str(c.get("path") or ""), str(c.get("name") or ""))


def _normalize_cookie(c: dict[str, Any]) -> dict[str, Any]:
    """裁剪为 Playwright add_cookies 可接受的字段, 补齐 domain/path 默认值。

    - 仅保留白名单字段(url 与 domain/path 在 add_cookies 中互斥, 故剔除 url);
    - domain 缺失时尝试从 url 推导; path 缺失默认 "/"; sameSite 默认 Lax。
    """
    out: dict[str, Any] = {}
    for f in _COOKIE_FIELDS:
        if c.get(f) is not None:
            out[f] = c[f]
    if not out.get("domain") and c.get("url"):
        try:
            host = urlsplit(str(c["url"])).hostname
            if host:
                out["domain"] = host
        except ValueError:
            pass
    out.setdefault("path", "/")
    out.setdefault("sameSite", "Lax")
    if out.get("domain") and not str(out["domain"]).startswith("."):
        out["domain"] = "." + str(out["domain"])
    return out


def _parse_document_cookie(text: str, url: str) -> list[dict[str, Any]]:
    """解析 document.cookie 字符串(name=value; ...), 依据页面 URL 补 domain/path/secure。

    document.cookie 读不到 HttpOnly, 只能作为其他采集路径的兜底。
    """
    if not text:
        return []
    try:
        host = urlsplit(url).hostname or ""
        secure = urlsplit(url).scheme == "https"
    except ValueError:
        host, secure = "", False
    out: list[dict[str, Any]] = []
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "value": value,
                "domain": host,
                "path": "/",
                "secure": secure,
                "httpOnly": False,
                "sameSite": "Lax",
            }
        )
    return out


# ---------------------------------------------------------- 鉴权凭据类型判断

def _classify_credential(key: Any, value: Any) -> dict[str, Any]:
    """判断某 key/value 是否属于鉴权凭据, 并给出类型(cookie/localStorage 通用)。

    JWT 站点常见把 token 放 localStorage/sessionStorage, 鉴权头并非一定在 cookie 中;
    返回 {"key", "value", "type"} 供 Agent 判断真实鉴权凭据来源。
    """
    k = str(key or "").lower()
    v = str(value or "")
    kind = "unknown"
    if v.startswith("eyJ"):
        kind = "jwt"
    elif any(
        x in k for x in ("jwt", "access_token", "refresh_token", "id_token", "token")
    ):
        kind = "token"
    elif any(x in k for x in ("authorization", "bearer", "auth")):
        kind = "authorization"
    elif any(x in k for x in ("session", "sid", "ssx", "ticket")):
        kind = "session"
    return {"key": str(key or ""), "value": value, "type": kind}


def _analyze_credentials(state: dict[str, Any]) -> dict[str, Any]:
    """从登录态快照分类鉴权相关凭据及来源(供 capture_login_state 返回)。"""
    creds: dict[str, Any] = {
        "cookies": [
            _classify_credential(c.get("name"), c.get("value"))
            for c in (state.get("cookies") or [])
            if isinstance(c, dict)
        ],
        "localStorage": [
            _classify_credential(k, v)
            for k, v in (state.get("localStorage") or {}).items()
        ],
        "sessionStorage": [
            _classify_credential(k, v)
            for k, v in (state.get("sessionStorage") or {}).items()
        ],
    }
    creds["summary"] = {
        "cookie": len(creds["cookies"]),
        "localStorage": len(creds["localStorage"]),
        "sessionStorage": len(creds["sessionStorage"]),
    }
    creds["hint"] = (
        "鉴权凭据不一定在 cookie 中: JWT 站点通常把 access_token/refresh_token 存 localStorage "
        "(或 sessionStorage), cookie 里可能只有会话标识(session/sid)。请结合目标站请求的鉴权头 "
        "(Authorization: Bearer xxx / Cookie) 判断真实来源, 需要时 cookie 与 storage 一并注入。"
    )
    return creds


class CrawlerEnv:
    """一次代码运行环境的默认函数容器, 关联当前 page 与后端运行配置。"""

    def __init__(self, cfg: Config, page: Any, context: Any = None,
                 login_gate: Any = None) -> None:
        self.cfg = cfg
        self.page = page
        self.context = context
        self._login_gate = login_gate
        self._saved: list[dict[str, Any]] = []
        self._mongo_client: Any = None
        self._ticket_coll: Any = None

    # ------------------------------------------------------------ 保存

    async def reset_saved(self) -> None:
        """清空上一轮运行保存的内容, 每次运行代码前调用。"""
        await asyncio.to_thread(shutil.rmtree, SAVED_DIR, True)
        self._saved = []

    def _limit_enabled(self) -> bool:
        return self.cfg.dev_limit

    def limit_items(self, data: Any, n: int | None = None) -> Any:
        """开发测试模式限制遍历长度, 生产模式(--no-dev-limit)原样返回。

        列表/元组取前 n 条(默认 max_items), 迭代器/生成器用 islice 惰性截取,
        避免爬取全量数据导致运行过长或 token 过多。n 显式传入时优先于默认值。
        """
        limit = n if n is not None else self.cfg.max_items
        if not self._limit_enabled() or limit is None or limit <= 0:
            return data
        if isinstance(data, (list, tuple)):
            return data[:limit]
        if isinstance(data, (dict, set, frozenset)):
            return list(data)[:limit]
        if isinstance(data, Iterator) or hasattr(data, "__next__"):
            return islice(data, limit)
        return data

    async def save_page(self) -> str:
        """保存当前页面的完整 HTML 到 tmp/saved, 返回文件绝对路径。"""
        html = await self.page.content()
        if self._limit_enabled() and self.cfg.max_bytes and self.cfg.max_bytes > 0:
            html = cap_text_bytes(
                html, self.cfg.max_bytes,
                notice=f"\n<!-- [已截断: 开发模式限制单次保存不超过 {self.cfg.max_bytes} 字节] -->",
            )
        return await self._save(html, "page", ".html")

    async def save_content(self, data: Any, fmt: str = "txt") -> str:
        """保存数据到 tmp/saved, 返回文件绝对路径。

        fmt 支持(默认纯文本):
          txt   纯文本(默认)
          json  JSON 数据(dict/list 自动序列化)
          jsonl 逐行 JSON(list[dict] 每行一条)
          csv   表格数据(list[dict] / list[list], 首行为表头)
          img   base64 图片(data URI 或纯 base64 字符串),
                文件后缀从 base64 图片字符串的 mime 类型读取

        开发测试模式(默认开启)下会自动限制数据量:
        - 列表/元组数据截断为前 max_items 条, txt 再按 max_bytes 截断;
        - 遍历爬取时可用 limit_items(data, n) 限制循环长度;
        - 同步上线时加 --no-dev-limit 取消限制。
        """
        fmt = normalize_fmt(fmt)
        max_items = self.cfg.max_items if self._limit_enabled() else None
        max_bytes = self.cfg.max_bytes if self._limit_enabled() else None
        ext, raw, display = prepare_save(data, fmt, max_items=max_items, max_bytes=max_bytes)
        return await self._save(raw, "img" if fmt == "img" else "content", ext, display)

    async def _save(self, data: str | bytes, kind: str, ext: str,
                    display: str | None = None) -> str:
        await asyncio.to_thread(SAVED_DIR.mkdir, parents=True, exist_ok=True)
        item_id = uuid.uuid4().hex[:8]
        name = f"{kind}_{item_id}{ext}"
        path = SAVED_DIR / name
        raw = data.encode("utf-8") if isinstance(data, str) else data
        await asyncio.to_thread(path.write_bytes, raw)
        self._saved.append(
            {
                "id": item_id,
                "kind": kind,
                "name": name,
                "path": str(path),
                "size": len(raw),
                "content": display if display is not None else (data if isinstance(data, str) else ""),
            }
        )
        return str(path)

    def saved_items(self) -> list[dict[str, Any]]:
        return self._saved

    # ---------------------------------------------------------- 登录协作

    async def page_login(self, method: str,
                         url: str = "",
                         account_selector: str = "",
                         password_selector: str = "",
                         captcha_selector: str = "",
                         send_selector: str = "",
                         submit_selector: str = "",
                         qr_selector: str = "",
                         timeout: float = 180) -> dict[str, Any]:
        """与用户协作完成登录(脚本会在此挂起, 等待用户扫码或填写模拟登录框)。

        - method="qr":      放大浏览器实时画面让用户扫码(不截图), 并持续监听登录跳转;
        - method="account": 弹出模拟登录框询问账号/密码(可选验证码), 提交后回填真实页面;
        - method="sms":     模拟登录框只询问账号/手机/邮箱, 支持"发送验证码"同步触发;

        **method 必填且必须显式指定为 qr/account/sms 之一, 不支持 "auto" 自动识别**;
        页面存在多种登录方式时应先用 ask_user 询问用户采用哪种, 再显式指定 method。
        - url="...":        登录页 URL。browser_run_code 每次重启为全新浏览器(初始 about:blank),
                            若当前页面不是登录页且给了 url, 会先导航到该登录页再交互,
                            交互期间不会变更/刷新页面; 未给 url 且页面非登录页时返回明确错误。

        各 selector 缺省时自动检测。返回 {"ok", "method", "url", "error"}。

        本函数仅负责唤起用户完成登录交互(扫码/账密/验证码), **不负责凭据的保存**;
        登录成功后是否把凭据存为长期记忆由业务代码自行调用 set_login_ticket(ticket, host)
        完成。
        """
        if self._login_gate is None:
            raise ValueError(
                "page_login 需要爬虫 Agent 会话支持, 请通过爬虫 Agent 的 "
                "browser_run_code / debug_code 运行"
            )
        method = (method or "").strip()
        if method not in ("qr", "account", "sms"):
            return {
                "ok": False,
                "method": method or "unknown",
                "url": url,
                "error": "page_login 的 method 必须显式指定为 qr/account/sms 之一, 不支持 auto, "
                         "请先用 ask_user 询问用户采用哪种登录方式再显式传入",
            }
        from .agent.login import LoginDetector, LoginCancelled

        url = (url or "").strip()
        cur = ""
        try:
            cur = str(self.page.url)
        except Exception:  # noqa: BLE001
            cur = ""
        # 全新浏览器初始是 about:blank; 当前不在登录页时, 先导航到给定的登录页 URL
        need_nav = bool(url) and not _same_page(url, cur)
        if need_nav:
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(0.6)
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "method": method,
                    "url": url,
                    "error": f"导航到登录页失败: {exc}",
                }
        info = await LoginDetector.analyze(
            self.page,
            method=method,
            account_selector=account_selector,
            password_selector=password_selector,
            captcha_selector=captcha_selector,
            send_selector=send_selector,
            submit_selector=submit_selector,
            qr_selector=qr_selector,
        )
        resolved = info.get("method") or "unknown"
        methods = [x for x in (info.get("methods") or []) if x in ("qr", "account", "sms")]
        # 页面必须真的识别出登录方式(否则可能停在 about:blank 或非登录页)
        if resolved not in ("qr", "account", "sms") or not methods:
            hint = ""
            if len(methods) > 1:
                hint = (
                    f"检测到多种登录方式({ '/'.join(methods) })，请先用 ask_user 询问用户采用哪种，"
                    "并在 page_login 中显式指定 method=qr/account/sms"
                )
            elif methods:
                hint = f"可用的登录方式: { '/'.join(methods) }，请显式指定 method"
            else:
                hint = "未识别出登录方式"
                if not url:
                    hint += (
                        "；当前页面不是登录页(browser_run_code 每次重启为全新浏览器, "
                        "初始可能停在 about:blank)，请先在脚本里 await page.goto(登录页URL) "
                        "再调用 page_login，或直接传入 url=登录页URL"
                    )
                elif not need_nav:
                    hint += "；已提供 url 但当前页面仍不是登录页，请确认 url 是否为登录页地址"
            return {"ok": False, "method": resolved, "url": info.get("url") or url, "error": hint}
        # 切换并确保目标登录方式可见(如"密码登录/短信登录/扫码登录"tab 未激活时自动点击)
        try:
            await LoginDetector.ensure_method(self.page, info, resolved)
        except Exception:  # noqa: BLE001
            pass
        payload = LoginDetector.build_payload(info, timeout=timeout)
        answers = await self._login_gate.request(payload)
        if answers.get("cancelled"):
            raise LoginCancelled("用户取消登录")
        if resolved == "qr":
            url = answers.get("url") or ""
            if not url:
                try:
                    url = str(self.page.url)
                except Exception:  # noqa: BLE001
                    url = ""
            await self._login_gate.finish("qr", url)
            return {"ok": True, "method": "qr", "url": url, "error": ""}
        try:
            await LoginDetector.fill_form(self.page, info, answers)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "method": resolved, "url": "", "error": f"回填表单失败: {exc}"}
        await LoginDetector.click_submit(self.page, info)
        result = await LoginDetector.wait_for_redirect(
            self.page, info.get("url") or "", timeout=timeout
        )
        if result is None:
            return {
                "ok": False,
                "method": resolved,
                "url": "",
                "error": "已提交登录, 但未检测到页面跳转, 请检查登录状态",
            }
        if result.get("fail"):
            return {
                "ok": False,
                "method": resolved,
                "url": "",
                "error": f"登录失败: {result.get('reason')}",
            }
        url = result.get("url") or ""
        await self._login_gate.finish(resolved, url)
        return {
            "ok": True,
            "method": resolved,
            "url": url,
            "error": "",
        }

    # ---------------------------------------------------------- 登录态快照(Application>Storage)

    async def capture_login_state(self) -> dict[str, Any]:
        """读取浏览器 Application>Storage 的登录态数据, 返回完整快照。

        对应 DevTools Application 面板的 Storage 部分:
        - cookies:        当前上下文全部 Cookie(含 HttpOnly), 由多路径兜底采集
                          (页面级 CDP Network.getCookies → context.cookies → document.cookie);
        - localStorage:   当前页面 localStorage 键值;
        - sessionStorage: 当前页面 sessionStorage 键值;
        - credentials:    鉴权凭据分类(cookie / localStorage / sessionStorage 中疑似
                          token/jwt/session/authorization 的项), 供判断"真正放行的鉴权凭据"
                          来自 cookie 还是 storage(JWT 站点常在 localStorage)。
        供 Agent 分析哪些是鉴权相关凭据, 或配合 set_login_ticket(ticket, host) 保存。
        """
        state = await self._capture_storage()
        state["credentials"] = _analyze_credentials(state)
        return state

    async def restore_login_state(self, state: dict[str, Any]) -> str:
        """把登录态快照恢复进当前浏览器(新浏览器也能直接拿到登录态)。

        - cookies:        context.add_cookies 注入(对后续请求生效);
        - localStorage / sessionStorage: 通过 add_init_script 在页面脚本执行前写入,
          并立即在当前页面写入, 兼容 SPA 启动时读取 storage 的情况。
        """
        await self._restore_storage(state if isinstance(state, dict) else {})
        n = (
            len((state or {}).get("cookies") or [])
            + len((state or {}).get("localStorage") or {})
            + len((state or {}).get("sessionStorage") or {})
        )
        return f"已恢复 {n} 项登录态(cookies/localStorage/sessionStorage)"

    async def _capture_storage(self) -> dict[str, Any]:
        state: dict[str, Any] = {"url": "", "cookies": [], "localStorage": {}, "sessionStorage": {}}
        try:
            state["url"] = str(self.page.url)
        except Exception:  # noqa: BLE001
            pass
        if self.context is not None:
            try:
                state["cookies"] = await self._capture_cookies()
            except Exception:  # noqa: BLE001
                state["cookies"] = []
        try:
            state["localStorage"] = await self.page.evaluate(
                "Object.fromEntries(Object.entries(localStorage))"
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            state["sessionStorage"] = await self.page.evaluate(
                "Object.fromEntries(Object.entries(sessionStorage))"
            )
        except Exception:  # noqa: BLE001
            pass
        return state

    async def _capture_cookies(self) -> list[dict[str, Any]]:
        """多路径采集 Cookie, 任一采集路径取不到值时自动降级到下一路径:

        1. 页面级 CDP `Network.getCookies(urls=[page.url])`: 含 HttpOnly,
           connect_over_cdp 下常规/无痕 profile 均可靠;
        2. `context.cookies([page.url])`: Playwright 网络层, 常规 profile 下可靠;
        3. `document.cookie` 解析: 仅非 HttpOnly, 最后兜底。

        遍历当前上下文全部页面, 按 (domain, path, name) 去重, 归一化为
        add_cookies 可接受的字段结构。某一条路径失败不影响整体结果。
        """
        pages: list[Any] = []
        if self.context is not None:
            pages = list(self.context.pages)
        if not pages:
            pages = [self.page]
        elif all(p is not self.page for p in pages):
            pages.insert(0, self.page)

        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for page in pages:
            url = ""
            try:
                url = str(page.url)
            except Exception:  # noqa: BLE001
                url = ""
            if not url or url.startswith("about:") or url.startswith("chrome://"):
                continue
            items: list[dict[str, Any]] = []
            # 路径 1: CDP Network.getCookies
            try:
                if self.context is not None:
                    sess = await self.context.new_cdp_session(page)
                    resp = await sess.send("Network.getCookies", {"urls": [url]})
                    items = resp.get("cookies") or []
            except Exception:  # noqa: BLE001
                items = []
            # 路径 2: Playwright context.cookies([url])
            if not items and self.context is not None:
                try:
                    items = await self.context.cookies([url])
                except Exception:  # noqa: BLE001
                    items = []
            for c in items:
                if not isinstance(c, dict):
                    continue
                c = _normalize_cookie(c)
                if c.get("domain") and c.get("name"):
                    merged[_cookie_key(c)] = c
        # 路径 3: document.cookie 兜底(仅非 HttpOnly, 补充 JS 写入的 cookie)
        for page in pages:
            url = ""
            try:
                url = str(page.url)
            except Exception:  # noqa: BLE001
                continue
            if not url or url.startswith("about:") or url.startswith("chrome://"):
                continue
            try:
                text = str(await page.evaluate("() => document.cookie"))
            except Exception:  # noqa: BLE001
                text = ""
            if not text:
                continue
            for c in _parse_document_cookie(text, url):
                if c.get("domain") and c.get("name"):
                    merged.setdefault(_cookie_key(c), c)
        return list(merged.values())

    async def _restore_storage(self, state: dict[str, Any]) -> None:
        cookies = state.get("cookies") or []
        if cookies and self.context is not None:
            # 归一化字段(剔除 url, 保证 domain/path 存在, 与 add_cookies 的互斥约束兼容)
            normalized = [_normalize_cookie(c) for c in cookies if isinstance(c, dict)]
            normalized = [
                c for c in normalized if c.get("name") is not None and c.get("domain")
            ]
            if normalized:
                try:
                    await self.context.add_cookies(normalized)
                except Exception:  # noqa: BLE001
                    pass
        ls = state.get("localStorage") or {}
        ss = state.get("sessionStorage") or {}
        for store, key in ((ls, "localStorage"), (ss, "sessionStorage")):
            if not store:
                continue
            js = (
                f"(() => {{ const d = {json.dumps(store, ensure_ascii=False)};"
                f" for (const k of Object.keys(d)) {{ try {{ {key}.setItem(k, d[k]); }} catch (e) {{}} }} }})()"
            )
            try:
                await self.page.add_init_script(js)
                await self.page.evaluate(js)
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------- 登录凭据

    async def _collection(self) -> Any:
        if self._mongo_client is None:
            from motor.motor_asyncio import AsyncIOMotorClient

            self._mongo_client = AsyncIOMotorClient(
                self.cfg.mongo_uri, serverSelectionTimeoutMS=3000
            )
            self._ticket_coll = self._mongo_client[self.cfg.mongo_db][TICKET_COLLECTION]
        return self._ticket_coll

    async def get_login_ticket(self, host: str) -> Any:
        """从 MongoDB 读取指定 host 下储存的 ticket, 未找到返回 None。

        仅接收 host 参数, 内部不对凭据做任何处理: 不注入浏览器、不查会话内存、
        不解析/推导类型, 直接把该 host 下储存的 ticket 原样返回。ticket 的获取
        与如何使用由用户脚本自行实现。
        """
        if not self.cfg.crawler_id:
            raise ValueError(
                "未配置 crawler_id, 无法获取登录凭据 (后端启动时通过 --crawler-id / CRAWLER_ID 指定)"
            )
        doc = await (await self._collection()).find_one(
            {"crawler_id": self.cfg.crawler_id, "host": host}
        )
        if doc is None or not doc.get("ticket"):
            return None
        return doc.get("ticket")

    async def set_login_ticket(self, ticket: Any, host: str) -> Any:
        """将 ticket 值直接储存在指定的 host 下 (不存在则新建)。

        仅接收 ticket 和 host 参数, 内部不对凭据做任何处理: 不提取浏览器登录态、
        不注入浏览器、不编码/解析类型, 直接把 ticket 值原样储存在指定的 host 下,
        返回写入的 ticket。
        """
        if not self.cfg.crawler_id:
            raise ValueError(
                "未配置 crawler_id, 无法保存登录凭据 (后端启动时通过 --crawler-id / CRAWLER_ID 指定)"
            )
        if ticket is None:
            raise ValueError("ticket 不能为空, 请传入要存储的登录凭据")
        await (await self._collection()).update_one(
            {"crawler_id": self.cfg.crawler_id, "host": host},
            {
                "$set": {
                    "ticket": ticket,
                    "updated_at": _now(),
                }
            },
            upsert=True,
        )
        return ticket

