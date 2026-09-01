"""backend.services.agent.login (LoginDetector / LoginGate) 测试。"""

from __future__ import annotations

import asyncio
import base64

import pytest

from conftest import FakePage


# --------------------------------------------------------------------------- 工具


def test_data_uri():
    from backend.services.agent.login import _data_uri

    out = _data_uri(b"\x89PNG")
    assert out.startswith("data:image/png;base64,")
    assert base64.b64decode(out.split(",", 1)[1]) == b"\x89PNG"


def test_pick_submit():
    from backend.services.agent.login import _pick_submit

    items = [
        {"sel": "#a", "vis": False, "text": "登录"},
        {"sel": "#b", "vis": True, "text": "点击登录"},
    ]
    picked = _pick_submit(items)
    assert picked["sel"] == "#b"
    # 无可见 -> 回退带选择器
    picked2 = _pick_submit([{"sel": "#a", "vis": False, "text": "登录"}])
    assert picked2["sel"] == "#a"
    # 无选择器 -> 回退任意 dict
    picked3 = _pick_submit([{"vis": True, "text": "ok"}])
    assert picked3["text"] == "ok"
    # 空
    picked4 = _pick_submit([])
    assert picked4 == {}


# --------------------------------------------------------------------------- LoginDetector


class RawPage(FakePage):
    """返回固定原始分析数据的假页面。"""

    def __init__(self, raw=None, url="http://login", **kwargs):
        super().__init__(url=url)
        self._raw = raw or {}
        self.clicked_tab = None
        self.eval_results = {}

    async def evaluate(self, expr, *args, **kwargs):
        from backend.services.agent.login import LOGIN_ANALYZE_JS

        self.evaluations.append(expr)
        if expr == LOGIN_ANALYZE_JS:
            return self._raw
        if expr in self.eval_results:
            return self.eval_results[expr]
        return await super().evaluate(expr, *args, **kwargs)


async def test_analyze_basic():
    from backend.services.agent.login import LoginDetector

    page = RawPage(raw={
        "methods": ["account", "qr"],
        "visible_methods": ["account"],
        "url": "http://login",
        "title": "登录",
        "user_inputs": [{"sel": "#u", "name": "account", "placeholder": "账号", "vis": True}],
        "password_inputs": [{"sel": "#p", "name": "password", "vis": True}],
        "captcha_inputs": [],
        "sms_send_buttons": [],
        "captcha_images": [],
        "submit_buttons": [{"sel": "#s", "text": "登录", "vis": True}],
    })
    info = await LoginDetector.analyze(page, method="account")
    assert info["method"] == "account"
    assert info["fields"][0]["key"] == "account"
    assert info["fields"][1]["key"] == "password"
    assert info["captcha"]["type"] == "none"


async def test_analyze_qr():
    from backend.services.agent.login import LoginDetector

    page = RawPage(raw={"methods": ["qr"], "visible_methods": ["qr"], "url": "http://l"})
    info = await LoginDetector.analyze(page, method="qr")
    assert info["method"] == "qr"
    # resolved 非 account/sms -> 提前返回
    assert "fields" not in info


async def test_analyze_auto_single_visible():
    from backend.services.agent.login import LoginDetector

    page = RawPage(raw={"methods": ["account", "qr"], "visible_methods": ["qr"],
                        "url": "http://l"})
    info = await LoginDetector.analyze(page, method="auto")
    assert info["method"] == "qr"


async def test_analyze_auto_single_method():
    from backend.services.agent.login import LoginDetector

    page = RawPage(raw={"methods": ["account"], "visible_methods": [],
                        "url": "http://l"})
    info = await LoginDetector.analyze(page, method="auto")
    assert info["method"] == "account"


async def test_analyze_auto_multi():
    from backend.services.agent.login import LoginDetector

    page = RawPage(raw={"methods": ["account", "qr"], "visible_methods": ["account", "qr"],
                        "url": "http://l"})
    info = await LoginDetector.analyze(page, method="auto")
    assert info["method"] == "multi"


async def test_analyze_evaluate_error():
    from backend.services.agent.login import LoginDetector

    class Boom:
        async def evaluate(self, js):
            raise RuntimeError("nav error")

        @property
        def url(self):
            return "http://l"

    info = await LoginDetector.analyze(Boom())
    assert info["method"] == "multi"
    assert info["url"] == "http://l"


async def test_analyze_sms():
    from backend.services.agent.login import LoginDetector

    page = RawPage(raw={
        "methods": ["sms"], "visible_methods": ["sms"], "url": "http://l",
        "user_inputs": [{"sel": "#phone", "name": "phone", "placeholder": "手机号", "vis": True}],
        "captcha_inputs": [{"sel": "#cap", "name": "captcha", "vis": True}],
        "sms_send_buttons": [{"sel": "#send", "text": "发送验证码", "vis": True}],
        "submit_buttons": [{"sel": "#s", "text": "登录", "vis": True}],
        "captcha_images": [],
    })
    info = await LoginDetector.analyze(page, method="sms")
    assert info["method"] == "sms"
    assert info["captcha"]["type"] == "sms"
    assert info["captcha"]["send_selector"] == "#send"
    assert info["submit_selector"] == "#s"


async def test_analyze_account_image_captcha():
    from backend.services.agent.login import LoginDetector

    page = RawPage(raw={
        "methods": ["account"], "visible_methods": ["account"], "url": "http://l",
        "user_inputs": [{"sel": "#u", "vis": True}],
        "password_inputs": [{"sel": "#p", "vis": True}],
        "captcha_images": [{"sel": "#capimg", "src": "http://img", "vis": True}],
        "submit_buttons": [],
    })
    info = await LoginDetector.analyze(page, method="account")
    assert info["captcha"]["type"] == "image"
    assert info["captcha"]["image"].startswith("data:image/png;base64,")
    assert info["captcha"]["refresh_selector"] == "#capimg"


async def test_analyze_account_sms_captcha():
    from backend.services.agent.login import LoginDetector

    page = RawPage(raw={
        "methods": ["account"], "visible_methods": ["account"], "url": "http://l",
        "user_inputs": [{"sel": "#u", "vis": True}],
        "password_inputs": [{"sel": "#p", "vis": True}],
        "captcha_inputs": [{"sel": "#cap", "vis": True}],
        "sms_send_buttons": [{"sel": "#send", "vis": True}],
        "submit_buttons": [],
    })
    info = await LoginDetector.analyze(page, method="account")
    assert info["captcha"]["type"] == "sms"
    assert info["captcha"]["input_selector"] == "#cap"


async def test_analyze_explicit_selectors():
    from backend.services.agent.login import LoginDetector

    page = RawPage(raw={})
    info = await LoginDetector.analyze(
        page, method="account",
        account_selector="#exact-u", password_selector="#exact-p",
        captcha_selector="#exact-c", send_selector="#exact-s",
        submit_selector="#exact-sub",
    )
    assert info["fields"][0]["selector"] == "#exact-u"
    assert info["fields"][1]["selector"] == "#exact-p"
    assert info["submit_selector"] == "#exact-sub"


async def test_click_method_tab():
    from backend.services.agent.login import LoginDetector

    page = RawPage()
    page.eval_results["true"] = True
    assert await LoginDetector.click_method_tab(page, "account") is False  # None 结果


async def test_ensure_method_qr_visible():
    from backend.services.agent.login import LoginDetector

    page = RawPage()
    page.eval_results["true"] = True
    await LoginDetector.ensure_method(page, {"method": "qr"}, "qr")
    # 可见则直接返回


async def test_ensure_method_account_switch():
    from backend.services.agent.login import LoginDetector

    page = RawPage()
    # 不可见 -> 点击 tab
    page.eval_results["false"] = False
    page.eval_results["true"] = True
    await LoginDetector.ensure_method(page, {"fields": [{"key": "password", "selector": "#p"}]}, "account")
    # 无选择器 -> 直接返回
    await LoginDetector.ensure_method(page, {"fields": []}, "account")


async def test_ensure_method_sms_no_selector():
    from backend.services.agent.login import LoginDetector

    page = RawPage()
    await LoginDetector.ensure_method(page, {"fields": [], "captcha": {}}, "sms")


def test_build_payload_qr():
    from backend.services.agent.login import LoginDetector

    payload = LoginDetector.build_payload({"method": "qr", "url": "http://l"}, timeout=60)
    assert payload["login_type"] == "qr"
    assert payload["zoom_browser"] is True
    assert payload["timeout"] == 60
    assert "fields" not in payload


def test_build_payload_account():
    from backend.services.agent.login import LoginDetector

    payload = LoginDetector.build_payload({
        "method": "account", "url": "http://l",
        "fields": [{"key": "account"}], "captcha": {"type": "none"},
        "submit_label": "登录",
    })
    assert payload["login_type"] == "account"
    assert payload["fields"] == [{"key": "account"}]


async def test_fill_form():
    from backend.services.agent.login import LoginDetector

    page = FakePage()
    info = {
        "fields": [{"key": "account", "selector": "#u"},
                   {"key": "password", "selector": "#p"}],
        "captcha": {"input_selector": "#c"},
    }
    ok = await LoginDetector.fill_form(page, info, {
        "account": "u1", "password": "p1", "captcha": "c1"})
    assert ok is True
    assert page.fills == [("#u", "u1"), ("#p", "p1"), ("#c", "c1")]
    # 空 answers 不填
    await LoginDetector.fill_form(page, info, {})
    assert len(page.fills) == 3


async def test_click_submit():
    from backend.services.agent.login import LoginDetector

    page = FakePage()
    await LoginDetector.click_submit(page, {"submit_selector": "#s"})
    await LoginDetector.click_submit(page, {})


async def test_wait_for_redirect_success(monkeypatch):
    from backend.services.agent.login import LoginDetector
    import backend.services.agent.login as lmod

    monkeypatch.setattr(lmod.asyncio, "sleep", lambda *a, **kw: _AwaitNone())
    page = FakePage(url="http://example.com/home")
    res = await LoginDetector.wait_for_redirect(page, "http://example.com/login", timeout=10)
    assert res == {"url": "http://example.com/home"}


async def test_wait_for_redirect_fail(monkeypatch):
    from backend.services.agent.login import LoginDetector
    import backend.services.agent.login as lmod

    monkeypatch.setattr(lmod.asyncio, "sleep", lambda *a, **kw: _AwaitNone())

    class FailPage(FakePage):
        async def evaluate(self, expr, *a, **kw):
            return "登录失败，验证码错误"

    res = await LoginDetector.wait_for_redirect(FailPage(url="http://l/login"), "http://l/login", timeout=10)
    assert res == {"fail": True, "reason": "验证码错误"}


async def test_wait_for_redirect_timeout(monkeypatch):
    from backend.services.agent.login import LoginDetector
    import backend.services.agent.login as lmod

    monkeypatch.setattr(lmod.asyncio, "sleep", lambda *a, **kw: _AwaitNone())
    page = FakePage(url="http://l/login")
    res = await LoginDetector.wait_for_redirect(page, "http://l/login", timeout=0.05)
    assert res is None


async def test_wait_for_redirect_url_error(monkeypatch):
    from backend.services.agent.login import LoginDetector
    import backend.services.agent.login as lmod

    monkeypatch.setattr(lmod.asyncio, "sleep", lambda *a, **kw: _AwaitNone())

    class BoomPage:
        @property
        def url(self):
            raise RuntimeError("gone")

        async def evaluate(self, expr, *a, **kw):
            return ""

    res = await LoginDetector.wait_for_redirect(BoomPage(), "", timeout=0.05)
    assert res is None


def test_navigated_away():
    from backend.services.agent.login import LoginDetector

    assert LoginDetector.navigated_away("http://a/x?q=1", "http://a/x#anchor") is False
    assert LoginDetector.navigated_away("http://a/login", "http://a/home") is True
    assert LoginDetector.navigated_away("", "") is False
    assert LoginDetector.navigated_away("http://a", "") is False


class _AwaitNone:
    def __await__(self):
        yield
        return None


# --------------------------------------------------------------------------- LoginGate


@pytest.fixture()
def gate(session, fake_bridge):
    from backend.services.agent.login import LoginGate

    return LoginGate(session, fake_bridge)


@pytest.fixture()
def fake_bridge():
    class Bridge:
        def __init__(self):
            self.eval_result = {"ok": True, "item": {"v": "http://home"}}
            self.shot = b"img-bytes"
            self.exprs = []

        async def evaluate(self, expr, timeout=10.0):
            self.exprs.append(expr)
            return self.eval_result

        async def element_shot(self, selector):
            return self.shot

    return Bridge()


async def test_gate_request_and_answer(session, gate):
    payload = {"kind": "login_request", "login_type": "account", "qid": "q1"}
    task = asyncio.create_task(gate.request(payload))
    await asyncio.sleep(0)
    assert session.status == "waiting"
    assert session.login == payload
    session.login_future.set_result({"account": "u"})
    answers = await task
    assert answers == {"account": "u"}
    assert session.status == "running"
    assert session.login is None


async def test_gate_request_qr_monitor(session, gate, fake_bridge):
    payload = {"kind": "login_request", "login_type": "qr", "qid": "q1",
               "url": "http://login", "timeout": 10}
    fake_bridge.eval_result = {"ok": True, "item": {"v": "http://home"}}
    task = asyncio.create_task(gate.request(payload))
    await asyncio.sleep(0.1)
    # monitor 检测到跳转 -> 自动完成
    answers = await asyncio.wait_for(task, timeout=2)
    assert answers == {"ok": True, "url": "http://home"}


async def test_gate_send_code(session, gate, fake_bridge):
    session.login = {"captcha": {"send_selector": "#send"}}
    res = await gate.send_code()
    assert res["ok"] is True
    assert "已触发" in res["message"]
    session.login = {"captcha": {}}
    res2 = await gate.send_code()
    assert res2["ok"] is False
    session.login = {"captcha": {"send_selector": "#s"}}
    fake_bridge.eval_result = {"ok": False, "error": "no page"}
    res3 = await gate.send_code()
    assert res3["ok"] is False


async def test_gate_refresh_captcha(session, gate, fake_bridge):
    session.login = {"captcha": {"refresh_selector": "#img", "image_selector": "#img"}}
    res = await gate.refresh_captcha()
    assert res["ok"] is True
    assert res["image"].startswith("data:image/png;base64,")
    session.login = {"captcha": {"image_selector": ""}}
    res2 = await gate.refresh_captcha()
    assert res2["ok"] is False


async def test_gate_refresh_qr(session, gate, fake_bridge):
    session.login = {"url": "http://login"}
    res = await gate.refresh_qr()
    assert res["ok"] is True
    assert "已刷新" in res["message"]
    ev = [e for e in session.hub._buffer if e["type"] == "login_action"][-1]
    assert ev["action"] == "refresh_qr"
    assert "location.reload" in fake_bridge.exprs[0]


async def test_gate_refresh_qr_fail(session, gate, fake_bridge):
    fake_bridge.eval_result = {"ok": False, "error": "no page"}
    res = await gate.refresh_qr()
    assert res["ok"] is False
    assert "失败" in res["message"]


async def test_gate_captcha_image(session, gate, fake_bridge):
    session.login = {"captcha": {"image_selector": "#img"}}
    img = await gate._captcha_image()
    assert img.startswith("data:image/png;base64,")
    session.login = {"captcha": {}}
    assert await gate._captcha_image() is None
    fake_bridge.shot = None

    class BoomBridge:
        async def element_shot(self, selector):
            raise RuntimeError("shot fail")

    gate2 = _new_gate(session, BoomBridge())
    session.login = {"captcha": {"image_selector": "#img"}}
    assert await gate2._captcha_image() is None


def _new_gate(session, bridge):
    from backend.services.agent.login import LoginGate

    return LoginGate(session, bridge)


async def test_gate_monitor_qr_timeout(session, gate, fake_bridge):
    session.login_future = asyncio.get_running_loop().create_future()
    session.login = {"url": "http://login", "timeout": 0.01}
    await gate._monitor_qr({"url": "http://login", "timeout": 0.01})
    assert any("等待扫码超时" in e["content"]
               for e in session.hub._buffer if e.get("type") == "status")


async def test_gate_monitor_qr_no_future(session, gate):
    session.login_future = None
    await gate._monitor_qr({"url": "http://login", "timeout": 1})


async def test_gate_finish(session, gate):
    await gate.finish("qr", "http://home")
    assert session.hub._buffer[-1]["type"] == "login_success"


async def test_gate_persist_error(session, gate):
    def boom(session, event):
        raise RuntimeError("persist fail")

    session.persist = boom
    await gate._persist({"type": "x"})
