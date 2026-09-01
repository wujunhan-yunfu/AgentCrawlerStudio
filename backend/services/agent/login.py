"""登录协作模块: 登录类型识别(LoginDetector) + 与前端交互的桥(LoginGate)。

- LoginDetector: 在页面执行 LOGIN_ANALYZE_JS 识别登录类型(二维码/账密/验证码)与
  各输入框选择器, 归一化为 login_info; 并负责回填表单 / 点击登录 / 监听跳转。
  操作对象是带 evaluate / locator / url 的 playwright Page(脚本环境内)。
- LoginGate: 绑定 AgentSession + BrowserBridge, 供 page_login 挂起脚本、向前端发
  login_request 事件、等待用户扫码/填表、以及处理发送验证码等浏览器动作。
  挂起期间事件循环仍可响应 /agent/login-action、/agent/login-answer 等并发请求。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from urllib.parse import urlsplit
from typing import Any


class LoginCancelled(Exception):
    """用户取消登录: 脚本应立即终止, 由上层(run_code / Agent)转换为本次运行终止。"""


LOGIN_ANALYZE_JS = r"""
(() => {
    const q = s => Array.from(document.querySelectorAll(s));
    const txt = el => (el ? (el.textContent || '').trim() : '');
    const attr = (el, ...ns) => { for (const n of ns) { const v = el && el.getAttribute(n); if (v) return v; } return ''; };
    const like = (s, re) => re.test(s.toLowerCase());
    const sel = el => {
        if (!el) return null;
        if (el.id) return '#' + CSS.escape(el.id);
        const n = el.getAttribute('name'); if (n) return el.tagName.toLowerCase() + '[name="' + CSS.escape(n) + '"]';
        const p = el.getAttribute('placeholder'); if (p) return el.tagName.toLowerCase() + '[placeholder="' + CSS.escape(p) + '"]';
        return null;
    };
    const vis = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        if (el.offsetParent === null && el.tagName !== 'BODY' && el.tagName !== 'HTML') return false;
        return r.top < innerHeight && r.left < innerWidth;
    };
    const inputs = q('input').filter(i => !(i.type === 'hidden') && !(i.type === 'submit'));
    const pass = q('input[type=password]');
    const user = inputs.filter(i => !(i.type === 'password') &&
        like(attr(i, 'name') + ' ' + (i.placeholder || ''), /(user|account|email|phone|mobile|username|用户名|账号|帐号|用户|手机|邮箱)/));
    const cap = inputs.filter(i => like(attr(i, 'name') + ' ' + (i.placeholder || ''), /(captcha|verify|验证码|校验码)/));
    const sms = q('button,a,.btn,[role=button]').filter(el => like(txt(el), /(发送验证码|获取验证码|获取短信|发送短信|获取动态码|Send code|Get code|Resend)/));
    // 登录方式 tab/入口文案检测(用于发现页面上存在哪些登录方式)
    const tabMatch = pat => q('div,li,a,span,button,p').filter(el => {
        const s = txt(el); return s.length >= 2 && s.length <= 24 && like(s, pat);
    }).slice(0, 6);
    const qrTabs = tabMatch(/(扫码登录|扫一扫|二维码|扫码|QR)/);
    const acctTabs = tabMatch(/(密码登录|账号登录|账密登录|用户名密码|账号登录)/);
    const smsTabs = tabMatch(/(短信登录|验证码登录|手机号登录|手机登录|动态码)/);
    // 二维码图片/画布(仅统计真实可见的)
    const qrImg = q('img,canvas').filter(el =>
        like(attr(el, 'src', 'class', 'id', 'alt'), /(qrcode|qr-code|qr_|二维码|扫码)/) && vis(el)).slice(0, 3);
    const qrTextVis = qrTabs.filter(vis);
    const capImg = q('img').filter(el =>
        like(attr(el, 'src', 'alt', 'class', 'id'), /(captcha|verify|kaptcha|验证码)/)).slice(0, 3);
    const submit = q('button[type=submit],input[type=submit],button,a[href]').filter(el =>
        like(txt(el) || attr(el, 'value'), /(登录|登入|sign in|log in|立即登录)/)).slice(0, 5);
    // 页面存在的全部登录方式
    const methods = [];
    if (pass.length || acctTabs.length) methods.push('account');
    if (sms.length || smsTabs.length || cap.length) methods.push('sms');
    if (qrImg.length || qrTextVis.length || qrTabs.length) methods.push('qr');
    if (!methods.length) methods.push('unknown');
    // 当前真正可见的登录方式(表单可见才算, 仅 tab 文案可见不算)
    const passVis = pass.filter(vis);
    const smsVis = sms.filter(vis);
    const capVis = cap.filter(vis);
    const visibleMethods = [];
    if (passVis.length) visibleMethods.push('account');
    if (smsVis.length && (capVis.length || acctTabs.filter(vis).length)) visibleMethods.push('sms');
    if (qrImg.length || qrTextVis.length) visibleMethods.push('qr');
    const uniq = a => a.filter((v, i) => a.indexOf(v) === i);
    const mInfo = el => ({ sel: sel(el), name: attr(el, 'name'), placeholder: attr(el, 'placeholder'), type: el.type || el.tagName.toLowerCase(), vis: vis(el) });
    return {
        url: location.href,
        title: document.title,
        has_password: pass.length > 0,
        methods: uniq(methods),
        visible_methods: uniq(visibleMethods),
        user_inputs: user.slice(0, 8).map(mInfo),
        password_inputs: pass.slice(0, 3).map(mInfo),
        captcha_inputs: cap.slice(0, 6).map(mInfo),
        sms_send_buttons: (smsVis.length ? smsVis : sms).slice(0, 5).map(el => ({ sel: sel(el), text: txt(el).slice(0, 30), vis: vis(el) })),
        qr_images: qrImg.map(el => ({ sel: sel(el), src: attr(el, 'src').slice(0, 80), cls: attr(el, 'class'), id: attr(el, 'id'), vis: vis(el) })),
        captcha_images: capImg.slice(0, 3).map(el => ({ sel: sel(el), src: attr(el, 'src').slice(0, 60), vis: vis(el) })),
        submit_buttons: submit.slice(0, 5).map(b => ({ sel: sel(b), text: (txt(b) || attr(b, 'value')).slice(0, 30), vis: vis(b) })),
        method_tabs: {
            qr: qrTabs.filter(vis).map(el => sel(el)).filter(Boolean).slice(0, 3),
            account: acctTabs.filter(vis).map(el => sel(el)).filter(Boolean).slice(0, 3),
            sms: smsTabs.filter(vis).map(el => sel(el)).filter(Boolean).slice(0, 3),
        },
    };
})()
"""

_CAPTCHA_FAIL_KEYWORDS = (
    "验证码错误",
    "验证码输入错误",
    "验证码已过期",
    "验证码不正确",
    "账号或密码错误",
    "用户名或密码错误",
    "账号或密码不正确",
    "账号不存在",
    "手机号未注册",
    "密码错误",
)


def _data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


# 刷新二维码后把登录页切回「扫码登录」tab(部分站点默认停在账密/短信 tab)
_QR_TAB_JS = r"""(() => {
    const labels = ['扫码登录', '扫一扫', '二维码登录', '二维码', 'QR登录'];
    for (const lb of labels) {
        const el = Array.from(document.querySelectorAll('div,li,a,span,button,p')).find(
            e => (e.textContent || '').trim() === lb && e.getBoundingClientRect().width > 0);
        if (el) { el.click(); return true; }
    }
    return false;
})()"""


_LOGIN_TEXT_RE = re.compile(r"(登录|登入|sign\s*in|log\s*in|立即登录)", re.IGNORECASE)


def _pick_submit(items: list) -> dict:
    """从候选提交按钮里挑最合适的: 优先可见且带选择器、文本含"登录"的。"""
    with_sel = [i for i in items if isinstance(i, dict) and i.get("sel")]
    vis = [i for i in with_sel if i.get("vis")]
    pool = vis if vis else with_sel
    if not pool:
        pool = [i for i in items if isinstance(i, dict)] or [{}]
    for i in pool:
        if _LOGIN_TEXT_RE.search(str(i.get("text") or "")):
            return i
    return pool[0]


class LoginDetector:
    """登录类型识别与表单归一化(操作对象为 playwright Page)。"""

    @staticmethod
    async def analyze(
        page: Any,
        method: str = "auto",
        account_selector: str = "",
        password_selector: str = "",
        captcha_selector: str = "",
        send_selector: str = "",
        submit_selector: str = "",
        qr_selector: str = "",
    ) -> dict[str, Any]:
        """识别登录类型并归一化为 login_info。method: auto/qr/account/sms。

        - 识别页面存在的全部登录方式(methods)与当前可见方式(visible_methods);
        - method="auto" 时: 只有一个可见方式用它; 多个可见方式 → resolved="multi"
          (由 Agent 先 ask_user 询问用户, 再显式传入 method);
        - 输入框/按钮优先取可见元素, 避免把隐藏 tab 里的短信/密码框误判进来。
        """
        raw: dict[str, Any] = {}
        try:
            r = await page.evaluate(LOGIN_ANALYZE_JS)
            if isinstance(r, dict):
                raw = r
        except Exception:  # noqa: BLE001
            raw = {}
        methods = raw.get("methods") or ["unknown"]
        visible = raw.get("visible_methods") or []
        m = (method or "auto").strip().lower()
        if m in ("qr", "account", "sms"):
            resolved = m
        elif m == "auto":
            # 仅当只有一种登录方式(或唯一可见)时才自动使用, 否则交给 Agent 询问用户
            if len(visible) == 1 and visible[0] in ("qr", "account", "sms"):
                resolved = visible[0]
            elif len(methods) == 1 and methods[0] in ("qr", "account", "sms"):
                resolved = methods[0]
            else:
                resolved = "multi"
        url = ""
        try:
            url = raw.get("url") or str(page.url)
        except Exception:  # noqa: BLE001
            url = ""
        info: dict[str, Any] = {
            "url": url,
            "title": raw.get("title") or "",
            "methods": [x for x in methods if x != "unknown"],
            "visible_methods": visible,
            "method": resolved,
        }
        if resolved not in ("account", "sms"):
            return info

        user_inputs = raw.get("user_inputs") or []
        pass_inputs = raw.get("password_inputs") or []
        cap_inputs = raw.get("captcha_inputs") or []
        sms_btns = raw.get("sms_send_buttons") or [{}]
        cap_imgs = raw.get("captcha_images") or [{}]
        sub_btns = raw.get("submit_buttons") or [{}]

        def pick(items: list, prefer_visible: bool = True,
                 match: str | None = None) -> dict[str, Any] | None:
            vis_items = [i for i in items if i.get("vis")]
            pool = vis_items if (prefer_visible and vis_items) else items
            if match:
                for i in pool:
                    blob = f"{i.get('name') or ''} {i.get('placeholder') or ''} {i.get('type') or ''}".lower()
                    if match in blob:
                        return i
                for i in items:
                    blob = f"{i.get('name') or ''} {i.get('placeholder') or ''} {i.get('type') or ''}".lower()
                    if match in blob:
                        return i
                return None
            return pool[0] if pool else None

        fields: list[dict[str, Any]] = []
        u_sel = account_selector or (pick(user_inputs) or {}).get("sel") or ""
        if u_sel:
            fields.append({
                "key": "account",
                "label": "账号/手机/邮箱",
                "input_type": "text",
                "selector": u_sel,
                "placeholder": (pick(user_inputs) or {}).get("placeholder") or "",
            })
        if resolved == "account":
            p_sel = password_selector or (pick(pass_inputs) or {}).get("sel") or ""
            if p_sel:
                fields.append({
                    "key": "password",
                    "label": "密码",
                    "input_type": "password",
                    "selector": p_sel,
                    "placeholder": (pick(pass_inputs) or {}).get("placeholder") or "",
                })
        info["fields"] = fields

        if resolved == "sms":
            # 验证码登录: 账号框优先手机号/邮箱(可能未激活 tab 而隐藏), 验证码输入与发送按钮必取
            u_sel = account_selector or (pick(user_inputs, prefer_visible=False, match="phone") or
                                         pick(user_inputs, prefer_visible=False, match="mobile") or
                                         pick(user_inputs, prefer_visible=False, match="email") or
                                         pick(user_inputs, prefer_visible=False) or {}).get("sel") or ""
            c_sel = captcha_selector or (pick(cap_inputs, prefer_visible=False) or {}).get("sel") or ""
            s_sel = send_selector or (pick(sms_btns, prefer_visible=False) or {}).get("sel") or ""
            if u_sel:
                info["fields"] = [{
                    "key": "account",
                    "label": "账号/手机/邮箱",
                    "input_type": "text",
                    "selector": u_sel,
                    "placeholder": (pick(user_inputs, prefer_visible=False) or {}).get("placeholder") or "",
                }]
            info["captcha"] = {
                "type": "sms",
                "input_key": "captcha",
                "input_selector": c_sel,
                "send_selector": s_sel,
            }
            info["submit_selector"] = submit_selector or (_pick_submit(sub_btns) or {}).get("sel") or ""
            info["submit_label"] = (_pick_submit(sub_btns) or {}).get("text") or "登录"
            return info

        # account: 验证码只认当前可见的(隐藏的短信验证码框不计入), 图形验证码优先截图
        vis_cap_sel = next((ci.get("sel") or "" for ci in cap_inputs if ci.get("vis")), "")
        c_sel = captcha_selector or vis_cap_sel
        vis_sms_sel = next((sb.get("sel") or "" for sb in sms_btns if sb.get("vis")), "")
        s_sel = send_selector or vis_sms_sel
        i_sel = (cap_imgs[0].get("sel") if cap_imgs else "") or ""
        cap: dict[str, Any] = {"type": "none"}
        if i_sel:
            cap = {
                "type": "image",
                "input_key": "captcha",
                "input_selector": c_sel,
                "image_selector": i_sel,
                "refresh_selector": i_sel,
            }
            try:
                data = await page.locator(i_sel).first.screenshot(timeout=5000)
                cap["image"] = _data_uri(data)
            except Exception:  # noqa: BLE001
                pass
        elif s_sel and c_sel:
            cap = {"type": "sms", "input_key": "captcha", "input_selector": c_sel, "send_selector": s_sel}
        elif c_sel:
            cap = {"type": "sms", "input_key": "captcha", "input_selector": c_sel, "send_selector": ""}
        info["captcha"] = cap
        info["submit_selector"] = submit_selector or (_pick_submit(sub_btns) or {}).get("sel") or ""
        info["submit_label"] = (_pick_submit(sub_btns) or {}).get("text") or "登录"
        return info

    @staticmethod
    async def click_method_tab(page: Any, method: str) -> bool:
        """点击页面上的登录方式 tab(如 密码登录/短信登录/扫码登录), 返回是否点击成功。"""
        js = r"""(method) => {
            const pats = {
                qr: ['扫码登录','扫一扫','二维码登录','二维码','QR登录'],
                account: ['密码登录','账号登录','账密登录','密码登录'],
                sms: ['短信登录','验证码登录','手机号登录','手机登录','动态码登录'],
            };
            const labels = (pats[method] || []);
            for (const lb of labels) {
                const el = Array.from(document.querySelectorAll('div,li,a,span,button,p')).find(
                    e => (e.textContent || '').trim() === lb && e.getBoundingClientRect().width > 0);
                if (el) { el.click(); return true; }
            }
            return false;
        }"""
        try:
            return bool(await page.evaluate(js, method))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def ensure_method(page: Any, info: dict[str, Any], method: str) -> None:
        """确保目标登录方式可见: 若对应表单/二维码不可见, 点击其 tab 切换过去。"""
        if method == "qr":
            vis = await page.evaluate(
                "Array.from(document.querySelectorAll('img,canvas')).some(i => {"
                " const r = i.getBoundingClientRect();"
                " return r.width > 50 && r.height > 50 &&"
                " /(qrcode|二维码|扫码)/i.test((i.getAttribute('src')||'')+(i.getAttribute('class')||'')+i.id); })"
            )
        else:
            sel = ""
            for f in info.get("fields") or []:
                if method == "sms" or f.get("key") == "password":
                    sel = f.get("selector") or ""
                    break
            if not sel:
                sel = (info.get("captcha") or {}).get("input_selector") or ""
            if not sel:
                return
            vis = await page.evaluate(
                f"(() => {{ const e = document.querySelector({sel!r}); if (!e) return true;"
                " const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && e.offsetParent !== null; })()"
            )
        if vis:
            return
        if await LoginDetector.click_method_tab(page, method):
            await asyncio.sleep(0.6)

    @staticmethod
    def build_payload(info: dict[str, Any], timeout: float = 180) -> dict[str, Any]:
        """构造 interrupt/login_request 载荷。二维码不传截图: 放大实时画面让用户扫码。"""
        base: dict[str, Any] = {
            "kind": "login_request",
            "qid": uuid.uuid4().hex[:12],
            "login_type": "qr" if info.get("method") == "qr" else "account",
            "method": info.get("method") or "auto",
            "url": info.get("url") or "",
            "zoom_browser": True,
            "timeout": timeout,
        }
        if info.get("method") == "qr":
            base["message"] = "请使用手机 APP 扫码登录，已放大浏览器实时画面并持续监听登录跳转"
            return base
        base["fields"] = info.get("fields") or []
        base["captcha"] = info.get("captcha") or {"type": "none"}
        base["submit_label"] = info.get("submit_label") or "登录"
        return base

    @staticmethod
    async def fill_form(page: Any, info: dict[str, Any], answers: dict[str, Any]) -> bool:
        """把用户提交的值回填到真实表单(React 兼容)。"""
        field_map = {f.get("key"): f for f in (info.get("fields") or [])}
        if "account" in field_map and answers.get("account"):
            sel = (field_map["account"] or {}).get("selector")
            if sel:
                await page.fill(sel, str(answers["account"]))
        if "password" in field_map and answers.get("password"):
            sel = (field_map["password"] or {}).get("selector")
            if sel:
                await page.fill(sel, str(answers["password"]))
        cap = info.get("captcha") or {}
        c_sel = cap.get("input_selector")
        if c_sel and answers.get("captcha"):
            await page.fill(c_sel, str(answers["captcha"]))
        return True

    @staticmethod
    async def click_submit(page: Any, info: dict[str, Any]) -> None:
        """点击登录/提交按钮: 优先显式选择器, 回退文本"登录"按钮, 再回退 form.requestSubmit()。"""
        sel = info.get("submit_selector") or ""
        if sel:
            try:
                await page.click(sel, timeout=8000)
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            await page.click("button:has-text('登录'):visible, button:has-text('登入'):visible", timeout=5000)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            await page.evaluate("document.querySelector('form')?.requestSubmit()")
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def wait_for_redirect(page: Any, start_url: str, timeout: float = 30) -> dict[str, Any] | None:
        """轮询跳转: host+path 变化视为成功; 失败文案提前返回 fail。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(1.0)
            try:
                url = str(page.url)
            except Exception:  # noqa: BLE001
                url = ""
            if url and LoginDetector.navigated_away(start_url, url):
                return {"url": url}
            try:
                text = await page.evaluate("document.body ? document.body.innerText.slice(0, 600) : ''")
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                for kw in _CAPTCHA_FAIL_KEYWORDS:
                    if kw in text:
                        return {"fail": True, "reason": kw}
        return None

    @staticmethod
    def navigated_away(a: str, b: str) -> bool:
        """比较 scheme+host+path 是否变化(忽略查询串/锚点抖动)。"""
        try:
            ua, ub = urlsplit(a or ""), urlsplit(b or "")
        except ValueError:
            return False
        if not ua.hostname or not ub.hostname:
            return False
        if (ua.scheme, ua.hostname, ua.path) != (ub.scheme, ub.hostname, ub.path):
            return True
        return False


class LoginGate:
    """page_login 与前端交互的桥: 事件发送 + 等待用户答复 + 浏览器动作。"""

    def __init__(self, session: Any, bridge: Any):
        self.session = session
        self.bridge = bridge

    # ---------------------------------------------------------- 交互主流程

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发 login_request 事件, 挂起等待用户答复; 返回 answers。"""
        session = self.session
        loop = asyncio.get_running_loop()
        session.login = payload
        session.status = "waiting"
        session.login_future = loop.create_future()
        session.emit(
            {
                "type": "login_request",
                **payload,
                "session_id": session.id,
                "crawler_id": session.crawler_id,
            }
        )
        await self._persist({"type": "login_request", "content": "", "meta": {"login": payload}})
        monitor = None
        if payload.get("login_type") == "qr":
            monitor = loop.create_task(self._monitor_qr(payload))
        try:
            answers = await session.login_future
        finally:
            if monitor is not None and not monitor.done():
                monitor.cancel()
            session.login_future = None
            session.login = None
        session.status = "running"
        return answers

    async def send_code(self) -> dict[str, Any]:
        """点击真实页面的"发送验证码"按钮(供 /agent/login-action 调用)。"""
        captcha = (self.session.login or {}).get("captcha") or {}
        sel = captcha.get("send_selector") or ""
        if not sel:
            return {"ok": False, "message": "未找到发送验证码按钮的选择器"}
        r = await self.bridge.evaluate(f"document.querySelector({sel!r})?.click(); true")
        ok = bool(isinstance(r, dict) and r.get("ok"))
        msg = "已触发发送验证码，请查看手机" if ok else f"触发失败: {r.get('error') if isinstance(r, dict) else '未知错误'}"
        await self._persist({"type": "login_action", "action": "send_code", "ok": ok, "message": msg})
        self.session.emit({"type": "login_action", "action": "send_code", "ok": ok, "message": msg})
        return {"ok": ok, "message": msg}

    async def refresh_captcha(self) -> dict[str, Any]:
        """点击真实页面的验证码图片刷新并重截(供 /agent/login-action 调用)。"""
        captcha = (self.session.login or {}).get("captcha") or {}
        sel = captcha.get("refresh_selector") or captcha.get("image_selector") or ""
        if sel:
            await self.bridge.evaluate(f"document.querySelector({sel!r})?.click(); true")
            await asyncio.sleep(0.6)
        image = await self._captcha_image()
        msg = "验证码已刷新" if image else "未找到图形验证码元素"
        await self._persist({"type": "login_action", "action": "refresh_captcha", "ok": bool(image), "message": msg})
        self.session.emit({"type": "login_action", "action": "refresh_captcha", "ok": bool(image), "message": msg})
        return {"ok": bool(image), "message": msg, "image": image}

    async def _captcha_image(self) -> str | None:
        captcha = (self.session.login or {}).get("captcha") or {}
        sel = captcha.get("image_selector") or ""
        if not sel:
            return None
        try:
            data = await self.bridge.element_shot(sel)
            return _data_uri(data)
        except Exception:  # noqa: BLE001
            return None

    async def refresh_qr(self) -> dict[str, Any]:
        """刷新二维码: 重新加载当前登录页以生成新二维码(供 /agent/login-action 调用)。

        部分站点扫码时间过长二维码会过期, 用户点击「刷新二维码」后重新加载登录页
        (再切回扫码 tab), 并保持 QR 监听继续, 防止扫码超时。
        """
        r = await self.bridge.evaluate("location.reload(); true")
        ok = bool(isinstance(r, dict) and r.get("ok"))
        await asyncio.sleep(0.8)
        await self.bridge.evaluate(_QR_TAB_JS)
        msg = "二维码已刷新，请用最新二维码重新扫码" if ok else "二维码刷新失败"
        await self._persist({"type": "login_action", "action": "refresh_qr", "ok": ok, "message": msg})
        self.session.emit({"type": "login_action", "action": "refresh_qr", "ok": ok, "message": msg})
        return {"ok": ok, "message": msg}

    # ---------------------------------------------------------- QR 监听

    async def _monitor_qr(self, payload: dict[str, Any]) -> None:
        session = self.session
        start = payload.get("url") or ""
        timeout = float(payload.get("timeout") or 180)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            await asyncio.sleep(1.5)
            fut = session.login_future
            if fut is None or fut.done():
                return
            if loop.time() > deadline:
                session.emit({"type": "status", "content": "等待扫码超时，请确认已扫码后点击\"完成扫码\"继续"})
                return
            r = await self.bridge.evaluate("location.href")
            cur = ""
            if isinstance(r, dict) and r.get("ok"):
                cur = (r.get("item") or {}).get("v") or ""
            if cur and LoginDetector.navigated_away(start, cur):
                fut.set_result({"ok": True, "url": cur})
                return

    async def finish(self, method: str, url: str) -> None:
        """登录成功完成: 向前端广播 login_success 并持久化。"""
        self.session.emit({"type": "login_success", "method": method, "url": url})
        await self._persist(
            {"type": "login_success", "content": "", "meta": {"method": method, "url": url}}
        )

    async def _persist(self, event: dict[str, Any]) -> None:
        persist = getattr(self.session, "persist", None)
        if persist is not None:
            try:
                await persist(self.session, event)
            except Exception:  # noqa: BLE001
                pass
