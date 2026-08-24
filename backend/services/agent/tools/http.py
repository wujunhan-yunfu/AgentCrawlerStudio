"""HTTP 探测工具: 免浏览器直接抓取/试探目标站。"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from ..core.text import cap_text

_MAX_BODY = 6000


def build_http_tools() -> list:
    """HTTP 请求探测工具。"""

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

    return [http_request]
