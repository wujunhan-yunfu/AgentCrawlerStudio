"""backend.services.runmode 运行模式测试。

覆盖: run_context / webhook / code_source / data_webhook / heartbeat /
runtime(_SaveManager / RunEnv / execute 注入启动器) / login_mode / run_mode。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mongomock_motor import AsyncMongoMockClient

from backend.config import Config
from backend.services.runmode import run_mode
from backend.services.runmode.code_source import NoCodeError
from backend.services.runmode.data_webhook import DataCollector
from backend.services.runmode.heartbeat import HeartbeatSender
from backend.services.runmode.login_mode import LoginRunManager
from backend.services.runmode.run_context import LoginComplete, RunContext
from backend.services.runmode.runtime import (
    RunEnv,
    _Launched,
    _MongoTicketStore,
    _SaveManager,
    _load_crawler_module,
    _same_page,
    execute,
    resolve_headless,
)
from backend.services.runmode.webhook import (
    WebhookError,
    WebhookSender,
    build_headers,
    sign,
)


async def _a(v: Any) -> Any:
    return v


def make_cfg(**kw) -> Config:
    cfg = Config()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


async def fake_launch(cfg, headless):
    from conftest import FakeBrowser, FakeContext, FakePlaywright

    context = FakeContext()
    page = await context.new_page()
    browser = FakeBrowser(context)
    pw = FakePlaywright(browser)
    return _Launched(pw, browser, context, page)


class FakeSender:
    """可记录的 WebhookSender 假对象。"""

    def __init__(self, enabled=True, responses=None):
        self._enabled = enabled
        self.responses = responses or {}
        self.sent: list[tuple[str, dict]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send_with_retry(self, event, payload):
        self.sent.append((event, payload))
        resp = self.responses.get(event, {})
        if resp.get("__raise__"):
            raise WebhookError(str(resp.get("__raise__")))
        return {"ok": True}


class MutablePage:
    """url 可变的极简 page(供 _wait_for_login / page_login 测试)。"""

    def __init__(self, url: str = "https://example.com/login"):
        self._url = url

    @property
    def url(self) -> str:
        return self._url

    def set_url(self, url: str) -> None:
        self._url = url

    async def goto(self, url: str, **kwargs) -> None:
        self._url = url


class LoginFakeStream:
    """login 模式用假 BrowserStream: 记录 on_credential_saved 并按配置返回结果。"""

    def __init__(self, result=None, delay=0.0, trigger_save=True):
        self.result = result or {"ok": True, "login_complete": True}
        self.delay = delay
        self.trigger_save = trigger_save
        self.calls: list[tuple[str, Any]] = []

    async def run_code(self, code, login_gate=None, restart=True, on_output=None,
                       on_credential_saved=None):
        self.calls.append((code, login_gate))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.trigger_save and on_credential_saved is not None:
            try:
                await on_credential_saved({"ticket": "t"}, "example.com")
            except LoginComplete:
                return {"ok": True, "output": "", "error": "", "saved": [], "login_complete": True}
        return self.result


@pytest.fixture()
def mongo_client():
    client = AsyncMongoMockClient()
    yield client
    client.close()


# ---------------------------------------------------------------------------
# run_context
# ---------------------------------------------------------------------------


def test_run_context_defaults():
    ctx = RunContext(crawler_id="c1", run_type="once")
    assert ctx.run_id
    assert ctx.process_id
    assert ctx.started_at > 0
    assert ctx.status == "pending"
    assert ctx.pid > 0
    assert ctx.mode == "run"


def test_run_context_set_status():
    ctx = RunContext(crawler_id="c1")
    ctx.set_status("running")
    assert ctx.status == "running"
    ctx.set_status("failed", error_type="login_failed", error_message="bad", error_detail="trace")
    assert ctx.finished_at >= ctx.started_at
    assert ctx.duration_ms >= 0
    assert ctx.error_type == "login_failed"


def test_run_context_to_dict():
    ctx = RunContext(crawler_id="c1", run_id="r1", project_id="p1", workspace_id="w1")
    ctx.set_status("succeeded")
    d = ctx.to_dict()
    assert d["run_id"] == "r1"
    assert d["crawler_id"] == "c1"
    assert d["status"] == "succeeded"
    assert d["error"] is None
    assert d["trigger"] == "once"
    ctx.set_status("failed", error_type="code_error", error_message="boom")
    d = ctx.to_dict()
    assert d["error"]["type"] == "code_error"
    assert d["error"]["message"] == "boom"


def test_login_complete_is_exception():
    assert issubclass(LoginComplete, Exception)


# ---------------------------------------------------------------------------
# webhook
# ---------------------------------------------------------------------------


def test_sign_deterministic_and_secret_sensitive():
    a = sign("secret", "123", '{"x":1}')
    b = sign("secret", "123", '{"x":1}')
    assert a == b
    assert a.startswith("sha256=")
    assert a != sign("other", "123", '{"x":1}')
    assert a != sign("secret", "124", '{"x":1}')


def test_build_headers():
    headers = build_headers("run.started", "dlv1", "123", '{}', "secret")
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Webhook-Event"] == "run.started"
    assert headers["X-Webhook-Delivery"] == "dlv1"
    assert headers["X-Webhook-Timestamp"] == "123"
    assert headers["X-Webhook-Signature"].startswith("sha256=")


async def test_webhook_disabled_returns_none():
    sender = WebhookSender("")
    assert sender.enabled is False
    assert await sender.send("run.started", {}) is None
    assert await sender.send_with_retry("run.started", {}) is None


async def test_webhook_send_success(monkeypatch):
    sender = WebhookSender("https://cdc/webhooks/runs", "secret")

    class FakeResp:
        status_code = 200
        text = json.dumps({"ok": True})

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            assert url == "https://cdc/webhooks/runs"
            assert headers["X-Webhook-Signature"].startswith("sha256=")
            return FakeResp()

    monkeypatch.setattr(
        "backend.services.runmode.webhook.httpx.AsyncClient",
        lambda timeout: FakeClient(),
    )
    resp = await sender.send("run.started", {"event": "run.started"})
    assert resp["ok"] is True


async def test_webhook_send_http_error(monkeypatch):
    sender = WebhookSender("https://cdc/x", "secret")

    class FakeResp:
        status_code = 500
        text = "boom"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            return FakeResp()

    monkeypatch.setattr(
        "backend.services.runmode.webhook.httpx.AsyncClient",
        lambda timeout: FakeClient(),
    )
    with pytest.raises(WebhookError):
        await sender.send("run.failed", {})


async def test_webhook_retry_success(monkeypatch):
    sender = WebhookSender("https://cdc/x", "secret", max_attempts=3)
    attempts = {"n": 0}

    class FakeResp:
        status_code = 200
        text = "{}"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("net down")
            return FakeResp()

    monkeypatch.setattr(
        "backend.services.runmode.webhook.httpx.AsyncClient",
        lambda timeout: FakeClient(),
    )
    await sender.send_with_retry("run.succeeded", {})
    assert attempts["n"] >= 2


async def test_webhook_retry_exhausted(monkeypatch):
    sender = WebhookSender("https://cdc/x", "secret", max_attempts=2)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            raise ConnectionError("net down")

    monkeypatch.setattr(
        "backend.services.runmode.webhook.httpx.AsyncClient",
        lambda timeout: FakeClient(),
    )
    with pytest.raises(WebhookError):
        await sender.send_with_retry("run.failed", {})


# ---------------------------------------------------------------------------
# code_source
# ---------------------------------------------------------------------------


class FakeCodeStore:
    def __init__(self, head=None, commit=None, error=None):
        self._head = head
        self._commit = commit
        self._error = error

    async def get_head(self, crawler_id):
        if self._error:
            raise self._error
        return self._head

    async def get_commit(self, crawler_id, head):
        return self._commit


async def test_load_head_code_success(monkeypatch):
    store = FakeCodeStore(head="abc", commit={"content": "print(1)"})
    monkeypatch.setattr("backend.services.runmode.code_source.CodeStore", lambda *a, **k: store)
    code = await run_mode.load_head_code(make_cfg(crawler_id="c1", mongo_uri="mongodb://x"))
    assert code == "print(1)"


async def test_load_head_code_no_head(monkeypatch):
    store = FakeCodeStore(head=None)
    monkeypatch.setattr("backend.services.runmode.code_source.CodeStore", lambda *a, **k: store)
    with pytest.raises(NoCodeError):
        await run_mode.load_head_code(make_cfg())


async def test_load_head_code_no_content(monkeypatch):
    store = FakeCodeStore(head="abc", commit={})
    monkeypatch.setattr("backend.services.runmode.code_source.CodeStore", lambda *a, **k: store)
    with pytest.raises(NoCodeError):
        await run_mode.load_head_code(make_cfg())


async def test_load_head_code_mongo_error(monkeypatch):
    store = FakeCodeStore(error=RuntimeError("no mongo"))
    monkeypatch.setattr("backend.services.runmode.code_source.CodeStore", lambda *a, **k: store)
    with pytest.raises(NoCodeError):
        await run_mode.load_head_code(make_cfg())


# ---------------------------------------------------------------------------
# data_webhook
# ---------------------------------------------------------------------------


def test_data_collector_disabled():
    collector = DataCollector(make_cfg(), RunContext(crawler_id="c1"), WebhookSender(""))
    assert collector.enabled is False
    assert collector.delivery_for("x") == "pending"


async def test_data_collector_sync_delivery():
    ctx = RunContext(crawler_id="c1", run_id="r1")
    sender = FakeSender()
    collector = DataCollector(make_cfg(), ctx, sender, sync=True)
    item = {"name": "content_x.json", "kind": "content", "fmt": "json",
            "size": 5, "content": '{"a":1}'}
    save_id = collector.save_id_for(item)
    await collector.collect(item)
    await collector.collect(item)  # 同 save_id, 幂等
    assert collector.delivery_for(save_id) == "delivered"
    assert len(sender.sent) == 2
    payload = sender.sent[0][1]
    assert payload["event"] == "data.saved"
    data = payload["data"]
    assert data["crawler_id"] == "c1"
    assert data["run_id"] == "r1"
    assert data["save_id"] == save_id
    assert data["seq"] == 1
    assert data["content_hash"].startswith("sha256:")
    assert data["content"] == '{"a":1}'


async def test_data_collector_async_flush():
    ctx = RunContext(crawler_id="c1", run_id="r1")
    sender = FakeSender()
    collector = DataCollector(make_cfg(), ctx, sender, sync=False)
    collector.start()
    item = {"name": "a.txt", "kind": "content", "fmt": "txt",
            "size": 3, "content": "abc"}
    save_id = collector.save_id_for(item)
    await collector.collect(item)
    await collector.flush()
    assert collector.delivery_for(save_id) == "delivered"
    await collector.stop()


async def test_data_collector_failure_recorded():
    ctx = RunContext(crawler_id="c1", run_id="r1")
    sender = FakeSender(responses={"data.saved": {"__raise__": "boom"}})
    collector = DataCollector(make_cfg(), ctx, sender, sync=True)
    item = {"name": "a.txt", "kind": "content", "fmt": "txt", "size": 3, "content": "abc"}
    save_id = collector.save_id_for(item)
    await collector.collect(item)
    assert collector.delivery_for(save_id) == "failed"


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_disabled():
    hb = HeartbeatSender(make_cfg(), RunContext(crawler_id="c1"), FakeSender(enabled=False))
    assert hb.enabled is False


def test_heartbeat_payload():
    ctx = RunContext(crawler_id="c1", run_id="r1", run_type="cron", cron="*/5 * * * *")
    hb = HeartbeatSender(make_cfg(), ctx, FakeSender(), interval=30)
    payload = hb._build_payload("alive")
    assert payload["crawler_id"] == "c1"
    assert payload["run_type"] == "cron"
    assert payload["cron"] == "*/5 * * * *"
    assert payload["status"] == "alive"
    assert payload["health"]["browser"] is True
    assert payload["pid"] > 0
    assert payload["uptime_ms"] >= 0


async def test_heartbeat_send_and_stop():
    ctx = RunContext(crawler_id="c1")
    sender = FakeSender()
    hb = HeartbeatSender(make_cfg(), ctx, sender, interval=3600)
    await hb._send("alive")
    assert len(sender.sent) == 1
    assert sender.sent[0][0] == "heartbeat"
    await hb.stop()
    assert ctx.process_status == "stopping"
    assert any(e == "heartbeat" for e, _ in sender.sent)


# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------


def test_same_page():
    assert _same_page("https://a.com/login", "https://a.com/login")
    assert _same_page("https://a.com/login", "https://a.com/home") is False
    assert _same_page("about:blank", "https://a.com/login") is False


def test_resolve_headless():
    assert resolve_headless(make_cfg(mode="run", headless=None)) is True
    assert resolve_headless(make_cfg(mode="login", headless=None)) is False
    assert resolve_headless(make_cfg(mode="run", headless=False)) is False


def test_load_crawler_module(tmp_path):
    mod = _load_crawler_module("await save_content(1)\n", tmp_path)
    assert callable(mod.run)


def test_save_manager_formats(tmp_path):
    saver = _SaveManager(True, 5, 1000, tmp_path)
    assert saver.limit_items([1, 2, 3, 4, 5, 6], 3) == [1, 2, 3]

    async def run():
        p1 = await saver.save_content({"a": 1}, "json")
        p2 = await saver.save_content("x", "txt")
        p3 = await saver.save_content("data:image/png;base64," + "aGVsbG8=", "img")
        return p1, p2, p3

    p1, p2, p3 = asyncio.run(run())
    assert p1.endswith(".json")
    assert p2.endswith(".txt")
    assert p3.endswith(".png")
    assert len(saver.saved) == 3
    kinds = {s["kind"] for s in saver.saved}
    assert kinds == {"content", "img"}


async def test_save_manager_on_saved(tmp_path):
    collected = []
    saver = _SaveManager(True, 5, 1000, tmp_path, on_saved=lambda item: collected.append(item))
    await saver.save_content("hello", "txt")
    assert len(collected) == 1
    assert collected[0]["content"] == "hello"


async def test_mongo_ticket_store(mongo_client, monkeypatch):
    monkeypatch.setattr(
        "backend.services.runmode.runtime.AsyncIOMotorClient",
        lambda uri, **kw: mongo_client,
    )
    cfg = make_cfg(crawler_id="c1", mongo_uri="mongodb://fake", mongo_db="crawler")
    store = _MongoTicketStore(cfg)
    assert await store.get("example.com") is None
    await store.set({"t": 1}, "example.com")
    assert await store.get("example.com") == {"t": 1}
    cfg2 = make_cfg(crawler_id="c2", mongo_uri="mongodb://fake", mongo_db="crawler")
    store2 = _MongoTicketStore(cfg2)
    assert await store2.get("example.com") is None
    with pytest.raises(ValueError):
        await store.set(None, "example.com")


async def test_run_env_login_ticket(mongo_client, monkeypatch):
    monkeypatch.setattr(
        "backend.services.runmode.runtime.AsyncIOMotorClient",
        lambda uri, **kw: mongo_client,
    )
    from conftest import FakeBrowser, FakeContext, FakePage

    cfg = make_cfg(crawler_id="c1", mongo_uri="mongodb://fake", mongo_db="crawler")
    tickets = _MongoTicketStore(cfg)
    context = FakeContext()
    page = FakePage()
    browser = FakeBrowser(context)
    env = RunEnv(cfg, page, context, browser, _SaveManager(True, 5, 1000, Path("/tmp")), tickets, False)
    await env.set_login_ticket({"a": 1}, "example.com")
    assert await env.get_login_ticket("example.com") == {"a": 1}


async def test_page_login_qr_navigation():
    cfg = make_cfg()
    page = MutablePage("https://example.com/login")
    env = RunEnv(cfg, page, None, None, _SaveManager(True, 5, 1000, Path("/tmp")),
                 _MongoTicketStore(cfg), False)
    task = asyncio.create_task(_navigate_later(page))
    result = await env.page_login(method="qr", url="https://example.com/login", timeout=5)
    await task
    assert result["ok"] is True
    assert result["method"] == "qr"


async def _navigate_later(page: MutablePage) -> None:
    await asyncio.sleep(0.2)
    page.set_url("https://example.com/home")


async def test_page_login_timeout():
    cfg = make_cfg()
    page = MutablePage("https://example.com/login")
    env = RunEnv(cfg, page, None, None, _SaveManager(True, 5, 1000, Path("/tmp")),
                 _MongoTicketStore(cfg), False)
    result = await env.page_login(method="qr", url="https://example.com/login", timeout=0.3)
    assert result["ok"] is False
    assert "超时" in result["error"]


async def test_page_login_invalid_method():
    cfg = make_cfg()
    page = MutablePage()
    env = RunEnv(cfg, page, None, None, _SaveManager(True, 5, 1000, Path("/tmp")),
                 _MongoTicketStore(cfg), False)
    result = await env.page_login(method="", url="https://example.com/login")
    assert result["ok"] is False


async def test_capture_restore_login_state():
    from conftest import FakeBrowser, FakeContext, FakePage

    cfg = make_cfg()
    context = FakeContext()
    page = FakePage("https://example.com/")
    browser = FakeBrowser(context)
    env = RunEnv(cfg, page, context, browser, _SaveManager(True, 5, 1000, Path("/tmp")),
                 _MongoTicketStore(cfg), False)
    state = await env.capture_login_state()
    assert state["cookies"] == []
    msg = await env.restore_login_state({
        "cookies": [{"name": "k", "value": "v", "domain": ".example.com", "path": "/"}]
    })
    assert "已恢复" in msg
    assert context.cookies_added


async def test_execute_success():
    cfg = make_cfg(crawler_id="c1", mongo_uri="mongodb://fake", mode="run")
    code = 'await save_content({"a": 1}, "json")\nawait save_page()\n'
    collected = []

    async def on_saved(item):
        collected.append(item)

    result = await execute(cfg, code, on_saved=on_saved, launcher=fake_launch)
    assert result["ok"] is True
    assert len(result["saved"]) == 2
    assert len(collected) == 2
    names = [it["name"] for it in result["saved"]]
    assert any(n.startswith("content_") for n in names)
    assert any(n.startswith("page_") for n in names)


async def test_execute_code_error():
    cfg = make_cfg(crawler_id="c1", mode="run")
    result = await execute(cfg, 'raise ValueError("boom")\n', launcher=fake_launch)
    assert result["ok"] is False
    assert "boom" in result["error"]


async def test_execute_verification_failed():
    cfg = make_cfg(crawler_id="c1", mode="run")
    result = await execute(cfg, 'raise VerificationFailed("verify")\n', launcher=fake_launch)
    assert result["ok"] is False
    assert "verify" in result["error"]


# ---------------------------------------------------------------------------
# login_mode
# ---------------------------------------------------------------------------


async def test_login_success(monkeypatch):
    cfg = make_cfg(crawler_id="c1", login_timeout=5)
    monkeypatch.setattr("backend.services.runmode.login_mode.load_head_code",
                        lambda c: _a("print(1)"))
    stream = LoginFakeStream(result={"ok": True, "login_complete": True})
    mgr = LoginRunManager()
    mgr.setup(cfg, stream)
    await mgr.start()
    await mgr._task
    assert mgr.status == "succeeded"
    assert not mgr.error


async def test_login_no_code(monkeypatch):
    cfg = make_cfg(crawler_id="c1", login_timeout=5)

    async def _fail(c):
        raise NoCodeError("no head")

    monkeypatch.setattr("backend.services.runmode.login_mode.load_head_code", _fail)
    mgr = LoginRunManager()
    mgr.setup(cfg, LoginFakeStream())
    await mgr.start()
    await mgr._task
    assert mgr.status == "failed"
    assert "no head" in mgr.error


async def test_login_not_saved(monkeypatch):
    cfg = make_cfg(crawler_id="c1", login_timeout=5)
    monkeypatch.setattr("backend.services.runmode.login_mode.load_head_code",
                        lambda c: _a("print(1)"))
    stream = LoginFakeStream(result={"ok": True, "login_complete": False}, trigger_save=False)
    mgr = LoginRunManager()
    mgr.setup(cfg, stream)
    await mgr.start()
    await mgr._task
    assert mgr.status == "failed"
    assert "未检测到凭据保存" in mgr.error


async def test_login_run_failed(monkeypatch):
    cfg = make_cfg(crawler_id="c1", login_timeout=5)
    monkeypatch.setattr("backend.services.runmode.login_mode.load_head_code",
                        lambda c: _a("print(1)"))
    stream = LoginFakeStream(result={"ok": False, "error": "登录失败"}, trigger_save=False)
    mgr = LoginRunManager()
    mgr.setup(cfg, stream)
    await mgr.start()
    await mgr._task
    assert mgr.status == "failed"
    assert "登录失败" in mgr.error


async def test_login_timeout(monkeypatch):
    cfg = make_cfg(crawler_id="c1", login_timeout=0.5)
    monkeypatch.setattr("backend.services.runmode.login_mode.load_head_code",
                        lambda c: _a("print(1)"))
    stream = LoginFakeStream(result={"ok": True}, delay=1.5, trigger_save=False)
    mgr = LoginRunManager()
    mgr.setup(cfg, stream)
    await mgr.start()
    await mgr._task
    assert mgr.status == "failed"
    assert "登录超时" in mgr.error


# ---------------------------------------------------------------------------
# run_mode
# ---------------------------------------------------------------------------


def test_resolve_urls():
    urls = run_mode.resolve_urls(make_cfg(webhook_url="https://cdc/api/v1/webhooks/runs"))
    assert urls == (
        "https://cdc/api/v1/webhooks/runs",
        "https://cdc/api/v1/webhooks/heartbeat",
        "https://cdc/api/v1/webhooks/data",
    )
    assert run_mode.resolve_urls(make_cfg()) == ("", "", "")
    urls = run_mode.resolve_urls(make_cfg(
        webhook_url="https://cdc/runs",
        heartbeat_url="https://cdc/hb",
        data_webhook_url="https://cdc/data",
    ))
    assert urls == ("https://cdc/runs", "https://cdc/hb", "https://cdc/data")


def test_build_context():
    ctx = run_mode.build_context(make_cfg(crawler_id="c1", run_id="r1", run_type="cron", cron="* * * * *"))
    assert ctx.crawler_id == "c1"
    assert ctx.run_id == "r1"
    assert ctx.mode == "run"
    assert ctx.run_type == "cron"


def test_classify_error():
    assert run_mode.classify_error("用户取消登录") == "login_failed"
    assert run_mode.classify_error("登录失败") == "login_failed"
    assert run_mode.classify_error("VerificationFailed: cap") == "verification_failed"
    assert run_mode.classify_error("Traceback: boom") == "code_error"


def test_saved_summary_local_and_remote():
    items = [{"name": "a.json", "kind": "content", "size": 2}]
    summary = run_mode._saved_summary(items, None)
    assert summary[0]["delivery"] == "local"

    ctx = RunContext(crawler_id="c1", run_id="r1")
    collector = DataCollector(make_cfg(), ctx, FakeSender(), sync=True)
    item = {"name": "a.json", "kind": "content", "fmt": "json", "size": 2, "content": "{}"}
    summary = run_mode._saved_summary([item], collector)
    assert summary[0]["save_id"] == collector.save_id_for(item)


async def test_run_once_success(monkeypatch):
    ctx = run_mode.build_context(make_cfg(crawler_id="c1", run_id="r1"))
    sender = FakeSender()
    collector = DataCollector(make_cfg(), ctx, WebhookSender(""), sync=False)
    tickets = _MongoTicketStore(make_cfg(crawler_id="c1", mongo_uri="mongodb://fake"))
    monkeypatch.setattr(run_mode, "load_head_code", lambda c: _a("await save_content(1)"))
    monkeypatch.setattr(run_mode, "execute",
                        lambda *a, **k: _a({"ok": True, "saved": [], "error": ""}))
    cfg = make_cfg(crawler_id="c1", run_id="r1")
    code = await run_mode._run_once(cfg, ctx, sender, collector, tickets, None)
    assert code == 0
    assert ctx.status == "succeeded"
    events = [e for e, _ in sender.sent]
    assert "run.started" in events
    assert "run.succeeded" in events


async def test_run_once_no_code(monkeypatch):
    ctx = run_mode.build_context(make_cfg(crawler_id="c1", run_id="r1"))
    sender = FakeSender()
    collector = DataCollector(make_cfg(), ctx, WebhookSender(""), sync=False)
    tickets = _MongoTicketStore(make_cfg(crawler_id="c1", mongo_uri="mongodb://fake"))

    async def _fail(c):
        raise NoCodeError("no head")

    monkeypatch.setattr(run_mode, "load_head_code", _fail)
    code = await run_mode._run_once(make_cfg(crawler_id="c1", run_id="r1"),
                                    ctx, sender, collector, tickets, None)
    assert code == 2
    assert ctx.status == "failed"
    assert ctx.error_type == "no_code"
    assert "run.failed" in [e for e, _ in sender.sent]


async def test_run_once_failed_login(monkeypatch):
    ctx = run_mode.build_context(make_cfg(crawler_id="c1", run_id="r1"))
    sender = FakeSender()
    collector = DataCollector(make_cfg(), ctx, WebhookSender(""), sync=False)
    tickets = _MongoTicketStore(make_cfg(crawler_id="c1", mongo_uri="mongodb://fake"))
    monkeypatch.setattr(run_mode, "load_head_code", lambda c: _a("x"))
    monkeypatch.setattr(run_mode, "execute",
                        lambda *a, **k: _a({"ok": False, "error": "登录失败", "saved": []}))
    code = await run_mode._run_once(make_cfg(crawler_id="c1", run_id="r1"),
                                    ctx, sender, collector, tickets, None)
    assert code == 1
    assert ctx.status == "failed"
    assert ctx.error_type == "login_failed"


async def test_cron_invalid():
    ctx = run_mode.build_context(make_cfg(crawler_id="c1", run_type="cron", cron="bad cron"))
    sender = FakeSender()
    collector = DataCollector(make_cfg(), ctx, WebhookSender(""), sync=False)
    tickets = _MongoTicketStore(make_cfg(crawler_id="c1", mongo_uri="mongodb://fake"))
    code = await run_mode._cron_loop(make_cfg(crawler_id="c1", run_type="cron", cron="bad"),
                                     ctx, sender, collector, tickets, None)
    assert code == 2


async def test_cron_loop_one_iteration(monkeypatch):
    from datetime import datetime, timedelta, timezone

    ctx = run_mode.build_context(make_cfg(crawler_id="c1", run_type="cron", cron="* * * * *"))
    sender = FakeSender()
    collector = DataCollector(make_cfg(), ctx, WebhookSender(""), sync=False)
    tickets = _MongoTicketStore(make_cfg(crawler_id="c1", mongo_uri="mongodb://fake"))

    calls = {"run": 0, "sleep": 0}
    real_sleep = asyncio.sleep

    def fake_next_runs(expr, count=5, base=None):
        return [datetime.now(timezone.utc) + timedelta(seconds=0.01)]

    async def fake_sleep(wait):
        calls["sleep"] += 1
        if calls["sleep"] >= 2:
            raise asyncio.CancelledError()
        await real_sleep(0.001)

    async def fake_run_once(*a):
        calls["run"] += 1
        ctx.set_status("succeeded")
        return 0

    monkeypatch.setattr(run_mode, "next_runs", fake_next_runs)
    monkeypatch.setattr(run_mode.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(run_mode, "_run_once", fake_run_once)
    code = await run_mode._cron_loop(make_cfg(crawler_id="c1", run_type="cron", cron="* * * * *"),
                                     ctx, sender, collector, tickets, None)
    assert code == 0
    assert calls["run"] >= 1


async def test_main_async_once(monkeypatch):
    ctx = run_mode.build_context(make_cfg(crawler_id="c1", run_id="r1"))
    sender = FakeSender()
    collector = DataCollector(make_cfg(), ctx, WebhookSender(""), sync=False)
    tickets = _MongoTicketStore(make_cfg(crawler_id="c1", mongo_uri="mongodb://fake"))

    async def fake_run_once(*a):
        ctx.set_status("succeeded")
        return 0

    monkeypatch.setattr(run_mode, "_run_once", fake_run_once)
    code = await run_mode._main_async(make_cfg(crawler_id="c1", run_id="r1"),
                                      ctx, sender, collector, None, tickets)
    assert code == 0


# ---------------------------------------------------------------------------
# runmode 路由
# ---------------------------------------------------------------------------


async def test_mode_endpoint():
    import httpx

    from conftest import make_test_app
    from backend.config import Config

    cfg = Config()
    cfg.mode = "login"
    app = make_test_app(cfg=cfg)
    async with httpx.ASGITransport(app=app) as transport:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/v1/mode")
            assert r.status_code == 200
            body = r.json()
            assert body["mode"] == "login"
            assert body["crawler_id"] == cfg.crawler_id


async def test_login_status_endpoint():
    import httpx

    from conftest import make_test_app
    from backend.config import Config
    from backend.services.runmode.login_mode import LoginRunManager

    cfg = Config()
    cfg.mode = "login"
    app = make_test_app(cfg=cfg)
    mgr = LoginRunManager()
    mgr.status = "running"
    mgr.run_id = "r1"
    app.state.login_run = mgr
    async with httpx.ASGITransport(app=app) as transport:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/v1/login/status")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "running"
            assert body["run_id"] == "r1"


async def test_login_status_404():
    import httpx

    from conftest import make_test_app

    app = make_test_app()
    async with httpx.ASGITransport(app=app) as transport:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/v1/login/status")
            assert r.status_code == 404