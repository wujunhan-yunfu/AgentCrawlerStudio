"""backend.services.crawler 测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- 纯函数


def test_now():
    from backend.services.crawler import _now

    s = _now()
    assert "T" in s and s.endswith("+00:00")


def test_same_page():
    from backend.services.crawler import _same_page

    assert _same_page("http://a.com/x", "http://a.com/x?q=1")
    assert _same_page("http://a.com/x", "http://a.com/y") is False
    assert _same_page("http://a.com/", "https://b.com/") is False
    assert _same_page("about:blank", "about:blank") is False
    assert _same_page("", "") is False
    assert _same_page("http://a.com/x", "") is False


def test_cookie_key():
    from backend.services.crawler import _cookie_key

    assert _cookie_key({"domain": ".example.com", "path": "/", "name": "n"}) == (
        "example.com", "/", "n")
    assert _cookie_key({"domain": "example.com", "path": "/x", "name": "n"}) == (
        "example.com", "/x", "n")


def test_normalize_cookie():
    from backend.services.crawler import _normalize_cookie

    c = _normalize_cookie({"name": "n", "value": "v", "domain": "example.com"})
    assert c["path"] == "/"
    assert c["sameSite"] == "Lax"
    assert c["domain"] == ".example.com"
    assert "url" not in c

    # 从 url 推导 domain
    c2 = _normalize_cookie({"name": "n", "value": "v", "url": "http://example.com/x"})
    assert c2["domain"] == ".example.com"

    # 已带前导点的 domain
    c3 = _normalize_cookie({"name": "n", "domain": ".example.com"})
    assert c3["domain"] == ".example.com"


def test_parse_document_cookie():
    from backend.services.crawler import _parse_document_cookie

    out = _parse_document_cookie("a=1; b=2", "https://example.com/x")
    assert len(out) == 2
    assert out[0]["domain"] == "example.com"
    assert out[0]["secure"] is True
    assert out[0]["httpOnly"] is False

    out2 = _parse_document_cookie("", "http://x")
    assert out2 == []
    out3 = _parse_document_cookie("bad-entry; =empty; c=3", "http://x/y")
    assert len(out3) == 1
    assert out3[0]["name"] == "c"
    # 非法 url
    out4 = _parse_document_cookie("a=1", "::::bad")
    assert out4[0]["domain"] == ""


def test_classify_credential():
    from backend.services.crawler import _classify_credential

    assert _classify_credential("token", "abc")["type"] == "token"
    assert _classify_credential("x", "eyJhbGciOiJIUzI1NiJ9...")["type"] == "jwt"
    assert _classify_credential("authorization", "Bearer x")["type"] == "authorization"
    assert _classify_credential("session", "s123")["type"] == "session"
    assert _classify_credential("sid", "s")["type"] == "session"
    assert _classify_credential("foo", "bar")["type"] == "unknown"
    assert _classify_credential("access_token", "at")["type"] == "token"
    assert _classify_credential("refresh_token", "rt")["type"] == "token"
    assert _classify_credential("random", "")["type"] == "unknown"


def test_analyze_credentials():
    from backend.services.crawler import _analyze_credentials

    state = {
        "cookies": [
            {"name": "session", "value": "abc"},
            "not-a-dict",
        ],
        "localStorage": {"token": "t1", "plain": "x"},
        "sessionStorage": {},
    }
    out = _analyze_credentials(state)
    assert out["summary"] == {"cookie": 1, "localStorage": 2, "sessionStorage": 0}
    assert out["cookies"][0]["type"] == "session"
    assert out["localStorage"][0]["type"] == "token"
    assert out["localStorage"][1]["type"] == "unknown"
    assert "hint" in out


# --------------------------------------------------------------------------- CrawlerEnv


@pytest.fixture()
def env(cfg, fake_page, fake_context, monkeypatch, tmp_path):
    import backend.services.crawler as cmod

    monkeypatch.setattr(cmod, "SAVED_DIR", tmp_path / "saved")
    return cmod.CrawlerEnv(cfg, fake_page, context=fake_context)


async def test_reset_saved(env):
    env._saved = [{"id": "1"}]
    await env.reset_saved()
    assert env._saved == []


def test_limit_enabled(env):
    assert env._limit_enabled() is True
    env.cfg.dev_limit = False
    assert env._limit_enabled() is False


def test_limit_items(env):
    env.cfg.max_items = 2
    assert env.limit_items([1, 2, 3, 4]) == [1, 2]
    assert env.limit_items((1, 2, 3, 4)) == (1, 2)
    assert env.limit_items({"a": 1, "b": 2, "c": 3}) == ["a", "b"]
    it = env.limit_items(iter([1, 2, 3, 4]))
    assert list(it) == [1, 2]
    assert env.limit_items(42) == 42
    # 显式 n
    assert env.limit_items([1, 2, 3], n=1) == [1]
    # 生产模式
    env.cfg.dev_limit = False
    assert env.limit_items([1, 2, 3, 4]) == [1, 2, 3, 4]


async def test_save_page(env):
    path = await env.save_page()
    assert "page_" in path and path.endswith(".html")
    assert len(env.saved_items()) == 1
    assert env.saved_items()[0]["kind"] == "page"
    # 限制截断
    env.cfg.max_bytes = 20
    path2 = await env.save_page()
    assert path2 != path


async def test_save_content_txt(env):
    path = await env.save_content("hello world")
    assert path.endswith(".txt")
    assert env.saved_items()[0]["content"] == "hello world"


async def test_save_content_json_and_img(env):
    path = await env.save_content([{"a": 1}], "json")
    assert path.endswith(".json")
    import base64

    png = base64.b64encode(b"\x89PNG").decode()
    p2 = await env.save_content(f"data:image/png;base64,{png}", "img")
    assert p2.endswith(".png")
    assert env.saved_items()[-1]["kind"] == "img"


async def test_save_content_invalid_fmt(env):
    with pytest.raises(ValueError):
        await env.save_content("x", "yaml")


async def test_save_content_dev_limits(env):
    env.cfg.max_items = 2
    env.cfg.max_bytes = 50
    path = await env.save_content(list(range(100)), "json")
    import json

    content = env.saved_items()[0]["content"]
    assert len(json.loads(content)) == 2


async def test_save_binary(env):
    path = await env._save(b"\x01\x02", "content", ".bin")
    assert path.endswith(".bin")
    assert env.saved_items()[0]["size"] == 2


async def test_page_login_no_gate(cfg, fake_page, fake_context):
    import backend.services.crawler as cmod

    env = cmod.CrawlerEnv(cfg, fake_page, context=fake_context)
    with pytest.raises(ValueError, match="page_login 需要"):
        await env.page_login("qr")


async def test_page_login_bad_method(env):
    env._login_gate = _FakeGate()
    res = await env.page_login("auto")
    assert res["ok"] is False
    assert "不支持 auto" in res["error"]


async def test_page_login_navigation_error(env, monkeypatch):
    async def fake_analyze(page, **kw):
        return {"method": "qr", "methods": ["qr"], "url": "http://login"}

    class BoomPage:
        @property
        def url(self):
            return "about:blank"

        async def goto(self, url, **kw):
            raise RuntimeError("goto failed")

    env.page = BoomPage()
    gate = _FakeGate()
    env._login_gate = gate
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze))
    res = await env.page_login("qr", url="http://login")
    assert res["ok"] is False
    assert "导航到登录页失败" in res["error"]


async def test_page_login_unknown_method(env, monkeypatch):
    async def fake_analyze(page, **kw):
        return {"method": "unknown", "methods": [], "url": ""}

    gate = _FakeGate()
    env._login_gate = gate
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze))
    res = await env.page_login("qr")
    assert res["ok"] is False
    assert "未识别出登录方式" in res["error"]

    # 多方法提示
    async def fake_analyze2(page, **kw):
        return {"method": "multi", "methods": ["qr", "account"], "url": ""}

    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze2))
    res2 = await env.page_login("qr")
    assert "多种登录方式" in res2["error"]

    # 单方法提示
    async def fake_analyze3(page, **kw):
        return {"method": "multi", "methods": ["qr"], "url": ""}

    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze3))
    res3 = await env.page_login("qr")
    assert "可用的登录方式" in res3["error"]

    # 已给 url 但仍不是登录页
    async def fake_analyze4(page, **kw):
        return {"method": "unknown", "methods": [], "url": "http://login"}

    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze4))
    env.page._url = "http://login"  # 当前已在登录页, need_nav=False
    res4 = await env.page_login("qr", url="http://login")
    assert "已提供 url" in res4["error"]


async def test_page_login_qr_success(env, monkeypatch):
    gate = _FakeGate()
    gate.answers = {"url": "http://after-login"}
    env._login_gate = gate

    async def fake_analyze(page, **kw):
        return {"method": "qr", "methods": ["qr"], "url": "http://login"}

    async def fake_ensure(page, info, method):
        return None

    async def fake_fill(page, info, answers):
        return True

    async def fake_click(page, info):
        return None

    async def fake_wait(page, start_url, timeout=30):
        return {"url": "http://after-login"}

    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.ensure_method", staticmethod(fake_ensure))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.fill_form", staticmethod(fake_fill))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.click_submit", staticmethod(fake_click))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.wait_for_redirect", staticmethod(fake_wait))

    res = await env.page_login("qr", url="http://login")
    assert res["ok"] is True
    assert res["url"] == "http://after-login"
    assert gate.finished == ("qr", "http://after-login")


async def test_page_login_cancelled(env, monkeypatch):
    from backend.services.agent.login import LoginCancelled

    gate = _FakeGate()
    gate.answers = {"cancelled": True}
    env._login_gate = gate

    async def fake_analyze(page, **kw):
        return {"method": "qr", "methods": ["qr"], "url": "http://login"}

    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze))
    with pytest.raises(LoginCancelled, match="用户取消登录"):
        await env.page_login("qr", url="http://login")


async def test_page_login_account_success(env, monkeypatch):
    gate = _FakeGate()
    gate.answers = {"account": "u", "password": "p", "captcha": "c"}
    env._login_gate = gate
    calls = {}

    async def fake_analyze(page, **kw):
        return {"method": "account", "methods": ["account"], "url": "http://login"}

    async def fake_ensure(page, info, method):
        calls["ensure"] = True

    async def fake_fill(page, info, answers):
        calls["fill"] = answers
        return True

    async def fake_click(page, info):
        calls["click"] = True

    async def fake_wait(page, start_url, timeout=30):
        return {"url": "http://home"}

    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.ensure_method", staticmethod(fake_ensure))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.fill_form", staticmethod(fake_fill))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.click_submit", staticmethod(fake_click))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.wait_for_redirect", staticmethod(fake_wait))

    res = await env.page_login("account", url="http://login")
    assert res["ok"] is True
    assert calls["fill"] == gate.answers
    assert gate.finished == ("account", "http://home")


async def test_page_login_no_redirect(env, monkeypatch):
    gate = _FakeGate()
    gate.answers = {"account": "u", "password": "p"}
    env._login_gate = gate

    async def fake_analyze(page, **kw):
        return {"method": "account", "methods": ["account"], "url": "http://login"}

    async def fake_fill(page, info, answers):
        return True

    async def fake_click(page, info):
        return None

    async def fake_wait(page, start_url, timeout=30):
        return None

    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.ensure_method", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.fill_form", staticmethod(fake_fill))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.click_submit", staticmethod(fake_click))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.wait_for_redirect", staticmethod(fake_wait))

    res = await env.page_login("account", url="http://login")
    assert res["ok"] is False
    assert "未检测到页面跳转" in res["error"]


async def test_page_login_fail_reason(env, monkeypatch):
    gate = _FakeGate()
    gate.answers = {"account": "u", "password": "p"}
    env._login_gate = gate

    async def fake_analyze(page, **kw):
        return {"method": "account", "methods": ["account"], "url": "http://login"}

    async def fake_fill(page, info, answers):
        return True

    async def fake_click(page, info):
        return None

    async def fake_wait(page, start_url, timeout=30):
        return {"fail": True, "reason": "密码错误"}

    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.ensure_method", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.fill_form", staticmethod(fake_fill))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.click_submit", staticmethod(fake_click))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.wait_for_redirect", staticmethod(fake_wait))

    res = await env.page_login("account", url="http://login")
    assert res["ok"] is False
    assert "密码错误" in res["error"]


async def test_page_login_fill_error(env, monkeypatch):
    gate = _FakeGate()
    gate.answers = {"account": "u"}
    env._login_gate = gate

    async def fake_analyze(page, **kw):
        return {"method": "account", "methods": ["account"], "url": "http://login"}

    async def fake_fill(page, info, answers):
        raise RuntimeError("fill boom")

    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.ensure_method", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.fill_form", staticmethod(fake_fill))
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.click_submit", staticmethod(lambda *a, **k: None))

    res = await env.page_login("account", url="http://login")
    assert res["ok"] is False
    assert "回填表单失败" in res["error"]


async def test_capture_storage(env):
    async def _evaluate(expr):
        if "localStorage" in expr:
            return {"k": "v"}
        if "sessionStorage" in expr:
            return {}
        return None

    page = SimpleNamespace(url="https://example.com/", evaluate=_evaluate)
    env.page = page
    state = await env._capture_storage()
    assert state["url"] == "https://example.com/"
    assert state["cookies"] == []
    assert state["localStorage"] == {"k": "v"}
    assert state["sessionStorage"] == {}


async def test_capture_login_state(env):
    state = await env.capture_login_state()
    assert "credentials" in state


async def test_capture_cookies_cdp(env, fake_context):
    class FakeCdpSession:
        async def send(self, method, params):
            assert method == "Network.getCookies"
            return {"cookies": [
                {"name": "a", "value": "1", "domain": "example.com", "path": "/"},
            ]}

    fake_context._cdp_session = FakeCdpSession()
    page = SimpleNamespace(url="https://example.com/",
                           evaluate=lambda *a: _AWAIT_EMPTY_DICT())
    env.context = fake_context
    env.page = page
    cookies = await env._capture_cookies()
    assert any(c["name"] == "a" for c in cookies)


async def test_capture_cookies_playwright(env, fake_context):
    class FakeCdpSession:
        async def send(self, method, params):
            raise RuntimeError("cdp fail")

    fake_context._cdp_session = FakeCdpSession()
    fake_context.cookies_added = []

    async def _cookies(urls):
        return [{"name": "b", "value": "2", "domain": "example.com", "path": "/"}]

    fake_context.cookies = _cookies
    page = SimpleNamespace(url="https://example.com/", evaluate=lambda *a: _AWAIT_EMPTY_DICT())
    env.context = fake_context
    env.page = page
    cookies = await env._capture_cookies()
    assert any(c["name"] == "b" for c in cookies)


async def test_capture_cookies_document(env, fake_context):
    class FakeCdpSession:
        async def send(self, method, params):
            raise RuntimeError("no cdp")

    fake_context._cdp_session = FakeCdpSession()
    fake_context.cookies_added = []

    async def _cookies(urls):
        return []

    fake_context.cookies = _cookies

    async def _evaluate(expr):
        return "doc=1"

    page = SimpleNamespace(url="https://example.com/", evaluate=_evaluate)
    env.context = fake_context
    env.page = page
    cookies = await env._capture_cookies()
    assert any(c["name"] == "doc" for c in cookies)


async def test_capture_cookies_no_pages(cfg, fake_page):
    import backend.services.crawler as cmod

    page = SimpleNamespace(url="about:blank", evaluate=lambda *a: _AWAIT_EMPTY_DICT())
    env = cmod.CrawlerEnv(cfg, page, context=None)
    assert await env._capture_cookies() == []


async def test_restore_storage(env, fake_context, fake_page):
    fake_context.cookies_added = []
    fake_page.evaluations = []
    state = {
        "cookies": [{"name": "n", "value": "v", "domain": "example.com", "url": "http://x"}],
        "localStorage": {"k": "v"},
        "sessionStorage": {},
    }
    await env._restore_storage(state)
    assert len(fake_context.cookies_added) == 1
    assert fake_context.cookies_added[0][0]["name"] == "n"
    assert any("localStorage" in e for e in fake_page.evaluations)


async def test_restore_login_state(env, fake_context, fake_page):
    res = await env.restore_login_state({"cookies": [], "localStorage": {}, "sessionStorage": {}})
    assert "已恢复 0 项登录态" in res
    res2 = await env.restore_login_state(None)
    assert "已恢复 0 项登录态" in res2


async def test_restore_storage_no_cookies_context(cfg, fake_page, monkeypatch, tmp_path):
    import backend.services.crawler as cmod

    monkeypatch.setattr(cmod, "SAVED_DIR", tmp_path / "saved")
    env = cmod.CrawlerEnv(cfg, fake_page, context=None)
    await env._restore_storage({"cookies": [{"name": "n"}], "localStorage": {}, "sessionStorage": {}})


# --------------------------------------------------------------------------- Mongo 登录凭据


class _FakeGate:
    def __init__(self):
        self.answers = {}
        self.finished = None

    async def request(self, payload):
        return self.answers

    async def finish(self, method, url):
        self.finished = (method, url)


async def _AWAIT_EMPTY_DICT():
    return {}


@pytest.fixture()
def mongo_env(cfg, fake_page, fake_context, monkeypatch, tmp_path):
    import backend.services.crawler as cmod

    monkeypatch.setattr(cmod, "SAVED_DIR", tmp_path / "saved")
    client = _FakeMongoClient()
    monkeypatch.setattr("motor.motor_asyncio.AsyncIOMotorClient", lambda uri, **kw: client)
    env = cmod.CrawlerEnv(cfg, fake_page, context=fake_context)
    return env, client


class _FakeMongoClient:
    def __init__(self):
        self.db = _FakeDB()

    def __getitem__(self, name):
        return self.db


class _FakeDB:
    def __init__(self):
        self.colls = {}

    def __getitem__(self, name):
        if name not in self.colls:
            self.colls[name] = _FakeColl()
        return self.colls[name]


class _FakeColl:
    def __init__(self):
        self.docs = {}

    async def find_one(self, query):
        key = (query.get("crawler_id"), query.get("host"))
        return self.docs.get(key)

    async def update_one(self, query, update, upsert=False):
        key = (query.get("crawler_id"), query.get("host"))
        self.docs[key] = update["$set"]


async def test_get_login_ticket_no_crawler_id(env):
    env.cfg.crawler_id = ""
    with pytest.raises(ValueError, match="crawler_id"):
        await env.get_login_ticket("example.com")


async def test_get_login_ticket_none(mongo_env):
    env, client = mongo_env
    assert await env.get_login_ticket("example.com") is None


async def test_get_login_ticket_found(mongo_env):
    env, client = mongo_env
    client.db["login_tickets"].docs[("dev_test", "example.com")] = {"ticket": "tok123"}
    assert await env.get_login_ticket("example.com") == "tok123"


async def test_set_login_ticket_no_crawler_id(env):
    env.cfg.crawler_id = ""
    with pytest.raises(ValueError, match="crawler_id"):
        await env.set_login_ticket("t", "host")


async def test_set_login_ticket_none_value(env):
    with pytest.raises(ValueError, match="ticket 不能为空"):
        await env.set_login_ticket(None, "host")


async def test_set_login_ticket_ok(mongo_env):
    env, client = mongo_env
    res = await env.set_login_ticket("tok", "example.com")
    assert res == "tok"
    assert client.db.colls["login_tickets"].docs[("dev_test", "example.com")]["ticket"] == "tok"


async def test_collection_cache(mongo_env):
    env, client = mongo_env
    coll1 = await env._collection()
    coll2 = await env._collection()
    assert coll1 is coll2


# --------------------------------------------------------------------------- 边界/异常分支


def test_same_page_invalid_url():
    from backend.services.crawler import _same_page

    assert _same_page("http://[bad", "http://x") is False
    assert _same_page("http://x", "http://[bad") is False


def test_normalize_cookie_invalid_url():
    from backend.services.crawler import _normalize_cookie

    c = _normalize_cookie({"name": "n", "url": "http://[bad"})
    assert c["path"] == "/"
    assert "domain" not in c


def test_parse_document_cookie_invalid_url():
    from backend.services.crawler import _parse_document_cookie

    out = _parse_document_cookie("a=1", "http://[bad")
    assert out[0]["domain"] == ""
    assert out[0]["secure"] is False


class _BoomURLPage:
    @property
    def url(self):
        raise RuntimeError("no url")

    async def goto(self, url, **kw):
        return None

    async def evaluate(self, expr):
        raise RuntimeError("no eval")


async def test_page_login_url_raise(env, monkeypatch):
    async def fake_analyze(page, **kw):
        return {"method": "qr", "methods": ["qr"], "url": "http://login"}

    gate = _FakeGate()
    gate.answers = {"url": "http://done"}
    env._login_gate = gate
    env.page = _BoomURLPage()
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze))
    res = await env.page_login("qr", url="http://login")
    assert res["ok"] is True
    assert res["url"] == "http://done"


async def test_page_login_qr_url_raise(env, monkeypatch):
    async def fake_analyze(page, **kw):
        return {"method": "qr", "methods": ["qr"], "url": "http://login"}

    gate = _FakeGate()
    gate.answers = {}  # 无 url → 尝试从 page.url 取, 抛异常 → url=""
    env._login_gate = gate
    env.page = _BoomURLPage()
    monkeypatch.setattr("backend.services.agent.login.LoginDetector.analyze", staticmethod(fake_analyze))
    res = await env.page_login("qr", url="http://login")
    assert res["ok"] is True
    assert res["url"] == ""
    assert gate.finished == ("qr", "")


async def test_capture_storage_exception_paths(env):
    env.page = _BoomURLPage()

    class BoomContext:
        @property
        def pages(self):
            raise RuntimeError("pages boom")

    env.context = BoomContext()
    state = await env._capture_storage()
    assert state["url"] == ""
    assert state["cookies"] == []
    assert state["localStorage"] == {}
    assert state["sessionStorage"] == {}


async def test_capture_storage_eval_raise(env, fake_context):
    class BoomEvalPage:
        @property
        def url(self):
            return "https://example.com/"

        async def evaluate(self, expr):
            raise RuntimeError("eval boom")

    env.context = fake_context
    env.page = BoomEvalPage()
    state = await env._capture_storage()
    assert state["localStorage"] == {}
    assert state["sessionStorage"] == {}


class _NoCdpContext:
    def __init__(self, pages):
        self._pages = pages

    @property
    def pages(self):
        return self._pages

    async def new_cdp_session(self, page):
        raise RuntimeError("no cdp")

    async def cookies(self, urls):
        return []


async def test_capture_cookies_insert_self_page(env):
    other = SimpleNamespace(url="https://other.com/", evaluate=lambda *a: _AWAIT_EMPTY_DICT())
    env.context = _NoCdpContext([other])
    env.page = SimpleNamespace(url="https://self.com/", evaluate=lambda *a: _AWAIT_EMPTY_DICT())
    assert await env._capture_cookies() == []


async def test_capture_cookies_both_paths_fail(env, fake_context):
    class FakeCdpSession:
        async def send(self, method, params):
            raise RuntimeError("cdp fail")

    fake_context._cdp_session = FakeCdpSession()

    async def _cookies(urls):
        raise RuntimeError("pw fail")

    fake_context.cookies = _cookies
    page = SimpleNamespace(url="https://example.com/", evaluate=lambda *a: _AWAIT_EMPTY_DICT())
    env.context = fake_context
    env.page = page
    assert await env._capture_cookies() == []


async def test_capture_cookies_non_dict_item(env, fake_context):
    class FakeCdpSession:
        async def send(self, method, params):
            return {"cookies": [
                "not-a-dict",
                {"name": "n", "value": "1", "domain": "example.com", "path": "/"},
            ]}

    fake_context._cdp_session = FakeCdpSession()
    page = SimpleNamespace(url="https://example.com/", evaluate=lambda *a: _AWAIT_EMPTY_DICT())
    env.context = fake_context
    env.page = page
    cookies = await env._capture_cookies()
    assert any(c["name"] == "n" for c in cookies)


async def test_capture_cookies_page_url_raise(env):
    env.context = _NoCdpContext([_BoomURLPage()])
    env.page = _BoomURLPage()
    assert await env._capture_cookies() == []


async def test_capture_cookies_doc_eval_raise(env):
    class BoomEvalPage:
        @property
        def url(self):
            return "https://example.com/"

        async def evaluate(self, expr):
            raise RuntimeError("eval boom")

    env.context = _NoCdpContext([BoomEvalPage()])
    env.page = BoomEvalPage()
    assert await env._capture_cookies() == []


async def test_restore_storage_add_cookies_raise(env, fake_context):
    async def _boom_add(cookies):
        raise RuntimeError("add fail")

    fake_context.add_cookies = _boom_add
    state = {
        "cookies": [{"name": "n", "value": "v", "domain": "example.com"}],
        "localStorage": {},
        "sessionStorage": {},
    }
    await env._restore_storage(state)  # 不抛异常


async def test_restore_storage_eval_raise(env, fake_context, fake_page):
    async def _boom_init(script):
        raise RuntimeError("init fail")

    fake_page.add_init_script = _boom_init
    fake_page.evaluations = []
    state = {"cookies": [], "localStorage": {"k": "v"}, "sessionStorage": {"s": "1"}}
    await env._restore_storage(state)  # 不抛异常
