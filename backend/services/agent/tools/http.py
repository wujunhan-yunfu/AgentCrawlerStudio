"""HTTP 工具: 页面快速探测(http_request) 与通用 HTTP 接口调用(api_request)。"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from ..core.text import cap_text

_MAX_BODY = 6000
_API_MAX_BODY = 8000
_MAX_TIMEOUT = 180.0

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

# 视为二进制、不直接回显正文的 content-type 前缀
_BINARY_TYPES = (
    "image/",
    "audio/",
    "video/",
    "font/",
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/gzip",
    "application/x-tar",
    "application/msword",
    "application/vnd.openxmlformats",
    "application/vnd.ms-",
)


def _loads_obj(raw: str | None, field: str) -> dict:
    """把 JSON 字符串解析为 dict(空串视为空对象)。"""
    if raw is None or not str(raw).strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 JSON 对象")
    return value


def _elapsed_ms(resp: Any) -> str:
    elapsed = getattr(resp, "elapsed", None)
    if elapsed is None:
        return "未知"
    try:
        return f"{elapsed.total_seconds() * 1000:.0f} ms"
    except Exception:  # noqa: BLE001
        return "未知"


def _format_body(resp: Any, fmt: str, limit: int) -> str:
    """按 content-type / 指定格式渲染响应体, 二进制只给摘要。"""
    ctype = (resp.headers.get("content-type") or "").lower()
    if any(t in ctype for t in _BINARY_TYPES):
        size = len(getattr(resp, "content", b"") or b"")
        return f"<二进制响应, {size} 字节, content-type: {ctype or '未知'}>"
    fmt = (fmt or "auto").lower()
    if fmt == "json" or (fmt == "auto" and "json" in ctype):
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return cap_text(resp.text, limit) + "\n[解析 JSON 失败, 已按原始文本返回]"
        return cap_text(json.dumps(data, ensure_ascii=False, indent=2), limit)
    return cap_text(resp.text, limit)


def build_http_tools() -> list:
    """HTTP 请求探测与通用接口调用工具。"""

    @tool
    async def http_request(
        method: str, url: str, headers: str = "{}", params: str = "{}", data: str = ""
    ) -> str:
        """直接发起 HTTP 请求(不需要浏览器), 返回状态码/响应头/响应体。

        用于快速判断目标网站: 能否直接抓取(无需登录/无反爬)、接口是否开放、
        需要哪些请求头等。method 取值 get/post/put/delete。headers/params 为
        JSON 字符串, data 为原始请求体。响应体截断到 6000 字符。
        Args:
            method: HTTP 方法, 如 get/post/put/delete。
            url: 请求 URL。
            headers: 请求头, JSON 字符串。
            params: URL 查询参数, JSON 字符串。
            data: 请求体, 原始字符串。
        Returns:
            返回状态码/请求头/响应头/响应体, 或错误信息。
        """
        try:
            import httpx

            h = json.loads(headers) if headers else {}
            p = json.loads(params) if params else {}
            body = data if data else None
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.request(
                    method.upper(), url, headers=h, params=p, content=body
                )
            body = resp.text[:_MAX_BODY]
            return (
                f"状态码: {resp.status_code}\n"
                f"请求头:\n{json.dumps(dict(resp.request.headers), ensure_ascii=False, indent=2)}\n"
                f"响应头:\n{json.dumps(dict(resp.headers), ensure_ascii=False, indent=2)}\n"
                f"响应体:\n{cap_text(body, _MAX_BODY)}"
            )
        except Exception as exc:  # noqa: BLE001
            return f"HTTP 请求失败: {exc}"

    @tool
    async def api_request(
        url: str,
        method: str = "GET",
        headers: str = "{}",
        params: str = "{}",
        json_body: str = "",
        form: str = "{}",
        data: str = "",
        cookies: str = "{}",
        timeout: float = 30.0,
        follow_redirects: bool = True,
        response_format: str = "auto",
        max_chars: int = 8000,
    ) -> str:
        """调用互联网上的任意 HTTP 接口(基于 httpx), 返回完整响应以便继续处理。

        与 http_request(仅快速探测网页能否直抓)不同, 本工具面向真实接口调用:
        支持自定义方法/请求头/查询参数/JSON 请求体/表单/原始报文/Cookie/超时,
        并返回最终 URL、状态码、耗时、重定向链路、请求头、响应头与响应体
        (JSON 自动格式化)。适用于调用 REST/开放 API、提交登录/查询参数、
        验证签名后的接口、对接 webhook 等场景。

        请求体三选一(同时提供时按 json_body > form > data 取用):
        json_body 为 JSON 字符串(以 application/json 发送), form 为 JSON 对象
        (application/x-www-form-urlencoded), data 为原始字符串报文。
        headers/params/cookies 均为 JSON 对象字符串。

        Args:
            url: 完整接口地址, 必须以 http:// 或 https:// 开头。
            method: HTTP 方法, GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS, 默认 GET。
            headers: 请求头, JSON 对象字符串, 如 {"Authorization": "Bearer x"}。
            params: URL 查询参数, JSON 对象字符串。
            json_body: JSON 请求体字符串, 如 {"page": 1}。
            form: 表单请求体, JSON 对象字符串。
            data: 原始请求体字符串(非 JSON/表单时使用)。
            cookies: Cookie, JSON 对象字符串。
            timeout: 超时秒数, 默认 30, 上限 180。
            follow_redirects: 是否自动跟随重定向, 默认 True。
            response_format: 响应体格式 auto/text/json, 默认 auto(按 content-type 判断)。
            max_chars: 响应体最多返回的字符数, 默认 8000。
        Returns:
            结构化的请求/响应信息(状态码、耗时、头、响应体), 或错误信息。
        """
        try:
            import httpx
        except Exception as exc:  # noqa: BLE001
            return f"HTTP 接口请求失败: httpx 不可用 ({exc})"

        url = str(url or "").strip()
        method = (method or "GET").strip().upper()
        if method not in _ALLOWED_METHODS:
            return f"HTTP 接口请求失败: 不支持的 method={method}, 可选 {'/'.join(sorted(_ALLOWED_METHODS))}"
        if not url.lower().startswith(("http://", "https://")):
            return "HTTP 接口请求失败: url 必须以 http:// 或 https:// 开头"

        has_json = bool(json_body and json_body.strip())
        try:
            h = {str(k): str(v) for k, v in _loads_obj(headers, "headers").items()}
            p = _loads_obj(params, "params")
            c = {str(k): str(v) for k, v in _loads_obj(cookies, "cookies").items()}
            f = {
                str(k): v if isinstance(v, (list, tuple)) else str(v)
                for k, v in _loads_obj(form, "form").items()
            }
            j = json.loads(json_body) if has_json else None
        except Exception as exc:  # noqa: BLE001
            return f"HTTP 接口请求失败: 参数解析错误 ({exc})"

        kwargs: dict[str, Any] = {"headers": h or None, "params": p or None}
        if c:
            kwargs["cookies"] = c
        if has_json:
            kwargs["json"] = j
        elif f:
            kwargs["data"] = f
        elif data:
            kwargs["content"] = data.encode("utf-8")

        fmt = (response_format or "auto").lower()
        if fmt not in {"auto", "text", "json"}:
            fmt = "auto"
        try:
            max_chars = max(200, min(int(max_chars or _API_MAX_BODY), 50000))
            timeout = max(1.0, min(float(timeout or 30.0), _MAX_TIMEOUT))
        except (TypeError, ValueError):
            return "HTTP 接口请求失败: timeout / max_chars 必须是数字"

        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=follow_redirects
            ) as client:
                resp = await client.request(method, url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return f"HTTP 接口请求失败: {type(exc).__name__}: {exc}"

        try:
            request_headers = dict(resp.request.headers)
        except Exception:  # noqa: BLE001
            request_headers = {}
        lines = [
            f"请求: {method} {url}",
            f"最终 URL: {getattr(resp, 'url', url)}",
            f"状态码: {resp.status_code} {getattr(resp, 'reason_phrase', '')}".rstrip(),
            f"耗时: {_elapsed_ms(resp)}",
        ]
        history = list(getattr(resp, "history", []) or [])
        if history:
            chain = " -> ".join(str(getattr(item, "url", "")) for item in history)
            lines.append(f"重定向: {chain} -> {getattr(resp, 'url', url)}")
        lines += [
            "请求头:",
            json.dumps(request_headers, ensure_ascii=False, indent=2),
            "响应头:",
            json.dumps(dict(resp.headers), ensure_ascii=False, indent=2),
            "响应体:",
            _format_body(resp, fmt, max_chars),
        ]
        return "\n".join(lines)

    return [http_request, api_request]
