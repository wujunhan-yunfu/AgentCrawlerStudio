"""backend.schemas 测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_navigate_request():
    from backend.schemas import NavigateRequest

    r = NavigateRequest(url="https://example.com")
    assert r.url == "https://example.com"
    assert r.new_page is False
    r2 = NavigateRequest(url="x", new_page=True)
    assert r2.new_page is True
    with pytest.raises(ValidationError):
        NavigateRequest()


def test_run_request():
    from backend.schemas import RunRequest

    r = RunRequest(code="print(1)")
    assert r.code == "print(1)"
    assert r.run_id is None
    assert RunRequest(code="a", run_id="abc").run_id == "abc"


def test_saved_item_and_run_result():
    from backend.schemas import RunResult, SavedItem

    item = SavedItem(id="1", kind="page", name="n", path="p", size=3, content="hi")
    assert item.size == 3
    res = RunResult(ok=True, output="o", error="")
    assert res.saved == []
    res2 = RunResult(ok=True, output="o", error="", saved=[item])
    assert len(res2.saved) == 1


def test_run_login_models():
    from backend.schemas import (
        RunLoginAnswerRequest,
        RunLoginCaptcha,
        RunLoginField,
        RunLoginRequest,
    )

    f = RunLoginField(key="account", label="账号")
    assert f.input_type == "text"
    assert f.placeholder is None
    cap = RunLoginCaptcha()
    assert cap.type == "none"
    req = RunLoginRequest()
    assert req.fields == []
    assert req.submit_label == "登录"
    req2 = RunLoginRequest(qid="q1", fields=[f], captcha=cap)
    assert req2.qid == "q1"
    assert req2.fields[0].key == "account"
    a = RunLoginAnswerRequest(answers={"account": "x"})
    assert a.answers["account"] == "x"


def test_format_and_page_models():
    from backend.schemas import FormatRequest, FormatResult, PageInfo

    assert FormatRequest(code="x").code == "x"
    r = FormatResult(ok=True, formatted="y", error="")
    assert r.formatted == "y"
    p = PageInfo(id="1", url="u", title="t")
    assert p.id == "1"


def test_capture_status():
    from backend.schemas import CaptureStatus

    s = CaptureStatus(running=True, error=None, viewers=0, fps=30.0,
                      frames_total=10, last_frame_age_ms=5.0)
    assert s.running is True
    assert s.fps == 30.0


def test_console_status():
    from backend.schemas import ConsoleStatus

    s = ConsoleStatus(targets=1, connections=1, subscribers={}, history=0)
    assert s.targets == 1


def test_eval_models():
    from backend.schemas import EvalItem, EvalRequest, EvalResult

    assert EvalRequest(expression="1+1").expression == "1+1"
    item = EvalItem(k="text", t="num", v="2")
    assert item.t == "num"
    res = EvalResult(ok=True, item=item)
    assert res.ok is True
    err = EvalResult(ok=False, error="boom")
    assert err.error == "boom"
    assert err.item is None
    assert err.stack is None


def test_properties_models():
    from backend.schemas import PropertiesRequest, PropertiesResult, PropertyEntry, EvalItem

    assert PropertiesRequest(object_id="o1").object_id == "o1"
    entry = PropertyEntry(name="a", item=EvalItem(k="text"))
    res = PropertiesResult(ok=True, props=[entry])
    assert res.props[0].name == "a"


def test_request_models():
    from backend.schemas import (
        CookieDeleteRequest,
        CookieSetRequest,
        DomBoxRequest,
        IdbDataRequest,
        IdbStoresRequest,
        NetworkBodyRequest,
        StorageItemsRequest,
        StorageSetRequest,
    )

    assert NetworkBodyRequest(request_id="r1").request_id == "r1"
    assert DomBoxRequest(backend_node_id=3).backend_node_id == 3
    si = StorageItemsRequest(origin="o")
    assert si.session is False
    ss = StorageSetRequest(origin="o", key="k")
    assert ss.value == ""
    assert ss.session is False
    c = CookieSetRequest(origin="o", name="n")
    assert c.path == "/"
    assert c.domain is None
    assert c.http_only is False
    assert c.secure is False
    d = CookieDeleteRequest(origin="o", name="n")
    assert d.name == "n"
    assert IdbStoresRequest(origin="o", database="db").database == "db"
    dreq = IdbDataRequest(origin="o", database="db", store="s")
    assert dreq.skip == 0
    assert dreq.count == 50


def test_status_result():
    from backend.schemas import CaptureStatus, StatusResult

    s = StatusResult(
        uptime=1.0, error=None, xvfb=True, chrome=True, chrome_cdp="http://x",
        capture=CaptureStatus(running=True, error=None, viewers=0, fps=1.0,
                              frames_total=1, last_frame_age_ms=1.0),
        cdp=None, pages=[],
    )
    assert s.chrome is True
