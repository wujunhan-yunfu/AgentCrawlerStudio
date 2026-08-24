"""浏览器桥接: 将 BrowserStream 的全异步控制方法暴露给 Agent 工具。

BrowserStream 已全面 async 化(async playwright / async httpx), 工具直接
await 调用即可, 全程无线程、无阻塞。
"""

from __future__ import annotations

import json
from typing import Any

from ..browser import BrowserStream

ANALYZE_JS = r"""
(() => {
    const t = () => document.title;
    const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 50)
        .map(a => ({ text: (a.textContent || '').trim().slice(0, 80), href: a.href }));
    const forms = Array.from(document.querySelectorAll('form')).map(f => ({
        action: f.action || f.getAttribute('action') || '',
        method: (f.method || 'get').toUpperCase(),
        inputs: Array.from(f.querySelectorAll('input,select,textarea,button')).map(i => ({
            type: i.getAttribute('type') || i.tagName.toLowerCase(),
            name: i.getAttribute('name') || i.id || '',
        })).slice(0, 20),
    }));
    const inputs = Array.from(document.querySelectorAll('input')).map(i => i.type || 'text');
    const hasPassword = inputs.includes('password');
    const buttons = Array.from(document.querySelectorAll('button')).map(b => (b.textContent || '').trim()).slice(0, 20);
    const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src).slice(0, 20);
    const bodyText = (document.body ? document.body.innerText : '') || '';
    const metaDesc = document.querySelector('meta[name="description"]');
    const renderHints = {
        nextData: !!document.querySelector('#__NEXT_DATA__'),
        nuxtData: !!document.querySelector('#__NUXT_DATA__'),
        rootApp: !!document.querySelector('#app, #root'),
        canvas: !!document.querySelector('canvas'),
        iframes: document.querySelectorAll('iframe').length,
        xhrSpa: /XMLHttpRequest|fetch\(/.test((document.querySelectorAll('script:not([src])')[0]?.textContent) || '') || undefined,
    };
    const url = location.href;
    const lowerUrl = url.toLowerCase();
    const loginUrlHint = /(login|signin|sign-in|auth|passport|logon|登\s*录|login\.php)/.test(lowerUrl);
    // 登录方式检测: 通过 tab/文案 + 表单可见性综合判断(通用, 不针对具体站点)
    const like = (s, re) => re.test(s.toLowerCase());
    const vis = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && el.offsetParent !== null &&
               r.top < innerHeight && r.left < innerWidth;
    };
    const allTxt = Array.from(document.querySelectorAll('div,li,a,span,button,p')).map(e => (e.textContent || '').trim()).filter(s => s.length >= 2 && s.length <= 24);
    const hasQrTab = allTxt.some(s => like(s, /(扫码登录|扫一扫|二维码|扫码|QR)/));
    const hasAcctTab = allTxt.some(s => like(s, /(密码登录|账号登录|账密登录)/));
    const hasSmsTab = allTxt.some(s => like(s, /(短信登录|验证码登录|手机号登录|手机登录)/));
    const visibleQr = Array.from(document.querySelectorAll('img,canvas')).some(i => vis(i) &&
        like((i.getAttribute('src') || '') + (i.getAttribute('class') || '') + i.id, /(qrcode|二维码|扫码)/));
    const loginMethods = [];
    if (hasPassword || hasAcctTab) loginMethods.push('account');
    if (hasSmsTab || /(发送验证码|获取验证码|获取短信)/.test(document.body ? document.body.innerText : '')) loginMethods.push('sms');
    if (hasQrTab || visibleQr) loginMethods.push('qr');
    const loginVisible = [];
    if (hasPassword && vis(document.querySelector('input[type=password]'))) loginVisible.push('account');
    if (visibleQr) loginVisible.push('qr');
    return JSON.stringify({
        url, title: t(), meta_description: metaDesc ? metaDesc.content.slice(0, 200) : null,
        text_length: bodyText.length,
        text_sample: bodyText.slice(0, 300),
        links, link_count: links.length, forms, form_count: forms.length,
        has_password_input: hasPassword,
        input_types: inputs.slice(0, 30),
        buttons: buttons.slice(0, 20), scripts,
        script_count: document.querySelectorAll('script').length,
        img_count: document.querySelectorAll('img').length,
        login_url_hint: loginUrlHint,
        login_methods: loginMethods,
        login_visible_methods: loginVisible,
        render: renderHints,
        ready_state: document.readyState,
    });
})()
"""


class BrowserBridge:
    """把后端现有浏览器能力暴露给 Agent, 不改变原有服务层逻辑。"""

    def __init__(self, stream: BrowserStream):
        self.stream = stream

    async def navigate(self, url: str, new_page: bool = False) -> dict:
        result = await self.stream.navigate(url, new_page)
        return {"url": result.get("url", url), "title": result.get("title", "")}

    async def pages(self) -> list[dict]:
        return await self.stream._cdp_pages()

    async def evaluate(self, expression: str, timeout: float = 10.0) -> dict:
        return await self.stream.cdp.evaluate(expression, timeout=timeout)

    async def element_shot(self, selector: str) -> bytes:
        """返回指定元素的原始截图字节(供登录图形验证码刷新)。"""
        return await self.stream.screenshot_element(selector)

    async def analyze_page(self) -> dict:
        result = await self.stream.cdp.evaluate(ANALYZE_JS, timeout=10.0)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "无法分析页面"}
        item = result.get("item") or {}
        try:
            return {"ok": True, "analysis": json.loads(item.get("v") or "{}")}
        except (ValueError, TypeError):
            return {"ok": False, "error": "页面分析结果解析失败"}

    async def run_code(self, code: str, timeout: float = 300.0,
                       login_gate: Any = None,
                       restart: bool = True) -> dict:
        return await self.stream.run_code(
            code, login_gate=login_gate, restart=restart
        )
