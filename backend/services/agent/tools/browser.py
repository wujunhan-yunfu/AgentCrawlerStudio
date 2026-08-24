"""浏览器控制工具: 导航 / 页面分析 / JS 求值 / 截图 / 整体运行爬虫脚本。

依赖 BrowserBridge 把真实浏览器能力暴露给 Agent。
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from ..core.fs import agent_sanitize
from ..core.text import cap_text
from ..session.model import AgentSession
from ..bridge import BrowserBridge
from ..login import LoginGate

_MAX_BODY = 6000


def build_browser_tools(session: AgentSession, bridge: BrowserBridge) -> list:
    """浏览器相关工具: 导航/列页/求值/截图/分析/整体运行。"""

    @tool
    async def browser_navigate(url: str, new_page: bool = False) -> str:
        """控制项目内置的浏览器导航到指定 URL。

        这是 Agent 操控浏览器的基础工具。导航成功后浏览器会停留在该页面,
        可用 page_analyze / browser_evaluate 继续分析页面源码。
        若判断目标站需要登录, 请先用 page_analyze 判断, 再考虑登录流程。
        Args:
            url: 要导航的目标 URL。
            new_page: 是否在新标签页打开, 默认 False(当前活动页)。
        Returns:
            成功返回当前页面 URL 与标题, 失败返回错误信息。
        """
        try:
            result = await bridge.navigate(url, new_page)
            return f"已导航到 {result.get('url')}, 标题: {result.get('title') or '(无标题)'}"
        except Exception as exc:  # noqa: BLE001
            return f"导航失败: {exc}"

    @tool
    async def browser_pages() -> str:
        """列出浏览器当前打开的所有标签页(URL 与标题)。

        用于了解当前浏览器状态, 判断是否已有目标页面, 或是否需要打开新页面。
        Args:
            无参数, 直接获取当前浏览器所有标签页。
        Returns:
            列表字符串, 每行一个标签页: "- 标题: URL"。
        """
        try:
            pages = await bridge.pages()
            if not pages:
                return "当前没有打开的标签页"
            return "\n".join(
                f"- {p.get('title') or '(无标题)'}: {p.get('url')}" for p in pages
            )
        except Exception as exc:  # noqa: BLE001
            return f"获取标签页失败: {exc}"

    @tool
    async def browser_evaluate(expression: str) -> str:
        """在当前活动页面执行一段 JavaScript 表达式, 返回执行结果。

        用于读取页面动态数据(如 window 全局变量、localStorage、接口返回值、
        渲染后的 DOM 内容等), 适合处理需登录或 JS 动态渲染后才能获取的数据。
        表达式结果必须是 JSON 可序列化或字符串; 返回 JSON.stringify 的结果。
        Args:
            expression: JavaScript 表达式, 可直接访问 window / document / DOM。
        Returns:
            执行结果字符串, 或错误信息。
        """
        result = await bridge.evaluate(expression)
        if not result.get("ok"):
            return f"执行失败: {result.get('error') or '未知错误'}"
        item = result.get("item") or {}
        return cap_text(item.get("v") or item.get("description") or "", _MAX_BODY)

    @tool
    async def page_analyze() -> str:
        """分析当前页面结构, 返回 JSON(URL/标题/链接/表单/按钮/脚本/是否需登录等)。

        用于判断: (1) 该站是否需要登录(密码输入框/登录 URL/鉴权); (2) 数据是
        静态 HTML 还是 JS 动态渲染(决定用 httpx 直接抓还是用浏览器渲染);
        (3) 候选数据入口(列表链接/翻页/接口)。返回 JSON 字符串。
        Args:
            无参数, 直接分析当前浏览器活动页面。
        Returns:
            JSON 字符串, 包括 URL/标题/链接/表单/按钮/脚本/是否需登录等信息, 或错误信息。
        """
        result = await bridge.analyze_page()
        if not result.get("ok"):
            return f"页面分析失败: {result.get('error')}"
        return json.dumps(result.get("analysis"), ensure_ascii=False, indent=2)

    @tool
    async def browser_run_code(code: str, restart: bool = True) -> str:
        """执行一段完整的 Python 爬虫脚本(playwright 自动化代码)。

        用于复杂爬取流程的整体运行: 登录->翻页->提取->保存。
        默认(restart=True)每次执行会重启全新无痕浏览器; 代码内可直接使用
        page / context / browser 对象, 以及内置函数 save_page() / save_content() /
        limit_items() / get_login_ticket() / set_login_ticket()。脚本为 async 风格,
        使用这些对象/函数时需加 await(如 `await page.goto(url)`、`await save_page()`),
        顶层 await 直接可用。小范围验证/试错优先用 debug_code
        只跑最小代码块, 代码块拼接完成后再用本工具整体运行确认与正式抓取。
        restart=False 时复用当前浏览器(不重启), 保留登录态/已打开页面, 用于登录后
        探查凭据、注入测试等需在"同一浏览器内连续操作"的场景(见登录必选流程)。
        开发测试模式下数据量会被限制(save_content 列表截断为 max_items 条、
        save_page 按 max_bytes 截断), 遍历爬取时应先用 limit_items(items)
        限制循环长度, 避免运行过长或 token 过多; 上线时会取消该限制。
        代码里的 print 输出会随结果返回, 保存的内容也会被记录。
        脚本内可直接调用 page_login(method) 引导用户交互登录(扫码/账密/验证码),
        method 必填且必须显式指定 qr/account/sms 之一, 不支持 auto;
        以及 get_login_ticket(host) / set_login_ticket(ticket, host)
        复用与保存登录凭据: get_login_ticket 只从 MongoDB 读取指定 host 下储存的
        ticket 返回(返回 None 表示无凭据), 不注入浏览器、不做任何处理;
        set_login_ticket 只把传入的 ticket 值直接储存在指定 host 下(crawler_id 关联),
         不提取浏览器登录态、不做任何处理; ticket 如何注入浏览器由业务代码自行实现,
         直接用 playwright 对象:
         cookies 用 `await context.add_cookies(...)`, localStorage/sessionStorage 用
         `await page.evaluate("localStorage.setItem(...)")` 注入;
         登录成功后提取凭据建议用 `await capture_login_state()`(含 HttpOnly cookie 多路径
         采集 + credentials 分类, 覆盖 cookie 与 localStorage/sessionStorage 中的
         token/jwt, 鉴权头来源不一定是 cookie)。
        需要登录时脚本登录段必须按**登录必选流程**编写: 先 get_login_ticket(host) 复用凭据 →
        取到则访问目标站→注入→page.reload() 刷新生效→校验; 取不到或凭据失效(先清空已注入信息
        context.clear_cookies() / localStorage.clear())则 page_login 自动导航登录页交互登录 →
        每次登录成功后都用 playwright 提取凭据并 set_login_ticket(ticket, host) 保存, 这样脚本
        只需登录一次, 之后每次运行自动复用凭据。page_login 仅负责唤起用户登录, 不会自动保存凭据,
        保存必须由业务代码显式调用 set_login_ticket 完成。
        注意: get_login_ticket 读取的凭据不完全可信, 注入刷新后需在目标站页面校验登录是否
        生效; 同一份旧凭据连续 3 次无法正常登录应立即弃用并清空注入, 改走 page_login 重新登录。
        Args:
            code: Python 源码字符串, 可直接使用内置浏览器对象与函数
            restart: 是否在运行前重启全新无痕浏览器, 默认 True; False 时复用当前浏览器
        Returns:
            执行结果字符串, 包括 ok/输出/错误/已保存内容等
        """
        result = await bridge.run_code(
            code, login_gate=LoginGate(session, bridge), restart=restart
        )
        saved = result.get("saved") or []
        if saved:
            session.emit({"type": "saved", "saved": saved})
        out = agent_sanitize(result.get("output") or "")
        err = agent_sanitize(result.get("error") or "")
        parts = [f"ok={result.get('ok')}"]
        if out:
            parts.append(f"输出:\n{cap_text(out, 3000)}")
        if err:
            parts.append(f"错误:\n{cap_text(err, 3000)}")
        if saved:
            parts.append(f"已保存 {len(saved)} 项内容")
        return "\n".join(parts)

    return [
        browser_navigate,
        browser_pages,
        browser_evaluate,
        page_analyze,
        browser_run_code,
    ]
