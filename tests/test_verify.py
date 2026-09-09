"""verify 包核心单测: 证据 / 类型判定 / 轨迹 / 求解器 / 协调器 / 运行期作用域。

使用假 ops / 假页面与注入的判定函数, 不依赖真实 Chrome/LLM。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend.config import Config
from backend.services.agent.verify import (
    EVIDENCE_JS,
    VerifyScope,
    humanize_trajectory,
    parse_evidence,
    to_data_uri,
)
from backend.services.agent.verify.classify import (
    Classifier,
    build_classifier,
    parse_model_output,
    result_from_model,
    rule_classify,
)
from backend.services.agent.verify.coordinator import (
    VerificationCoordinator,
    should_engage,
)
from backend.services.agent.verify.evidence import best_container, has_clues
from backend.services.agent.verify.exceptions import VerificationFailed


# --------------------------------------------------------------------------- helpers

def _clear_evidence(**kw) -> dict:
    ev = {
        "text": kw.get("text", "普通页面"),
        "text_hit": kw.get("text_hit", False),
        "iframes": kw.get("iframes", []),
        "candidates": kw.get("candidates", []),
        "ocr_hint": kw.get("ocr_hint", False),
        "url": kw.get("url", "https://example.com"),
        "viewport": [1280, 800],
    }
    return ev


def _slider_ev(**kw) -> dict:
    ev = _clear_evidence(text="请向右拖动滑块完成拼图", text_hit=True)
    ev["candidates"] = [
        {"x": 200, "y": 300, "w": 300, "h": 40, "vis": True},
    ]
    ev.update(kw)
    return ev


def _iframe_ev(**kw) -> dict:
    ev = _clear_evidence(
        text="hCaptcha", text_hit=True,
        iframes=[{"x": 100, "y": 100, "w": 300, "h": 76, "vis": True,
                  "src": "https://newassets.hcaptcha.com/captcha"}],
    )
    ev.update(kw)
    return ev


class RecordingOps:
    """协调器/求解器用的假 ops: 记录点击与拖动, 可切换证据。"""

    def __init__(self, evidence_fn):
        self.evidence_fn = evidence_fn
        self.clicks: list[tuple[float, float]] = []
        self.drags: list[tuple[float, float, float, float]] = []
        self.shots = 0

    async def evaluate(self, expression: str, timeout: float = 5.0) -> Any:
        return self.evidence_fn(expression)

    async def screenshot(self, region=None) -> bytes:
        self.shots += 1
        return b"shot"

    async def coords_click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))

    async def coords_drag(self, x0, y0, x1, y1, duration=0.6) -> None:
        self.drags.append((x0, y0, x1, y1))


def make_classifier(cfg, text: str | None = None):
    """注入返回固定 JSON 的判定函数。text=None 表示模型不可用(走规则)。"""

    async def _fn(evidence_text: str, image: bytes | None) -> Any:
        return text

    return build_classifier(cfg, classify_fn=_fn) if text is not None else build_classifier(cfg)


def _slider_ops():
    state = {"solved": False}

    def _ev(expr):
        if state["solved"]:
            return _clear_evidence()
        return _slider_ev()

    ops = RecordingOps(_ev)

    async def _drag(x0, y0, x1, y1, duration=0.6):
        ops.drags.append((x0, y0, x1, y1))
        state["solved"] = True

    ops.coords_drag = _drag
    return ops


# --------------------------------------------------------------------------- evidence / rules


def test_parse_evidence_forms():
    ev = parse_evidence(_clear_evidence())
    assert ev["viewport"] == [1280, 800]
    assert parse_evidence('{"text":"x","text_hit":true}')["text"] == "x"
    parsed = parse_evidence(json.dumps({"text": "y", "text_hit": True}))
    assert parsed["text_hit"] is True
    assert parse_evidence(None)["text"] == ""
    assert parse_evidence("not json {")["text"] == ""


def test_has_clues_and_best_container():
    assert has_clues(_clear_evidence()) is False
    assert has_clues(_iframe_ev()) is True
    ev = _clear_evidence(candidates=[{"x": 0, "y": 0, "w": 10, "h": 10, "vis": True}])
    assert has_clues(ev) is True
    # 优先候选元素(候选存在时选最大者)
    big = best_container(_clear_evidence(
        candidates=[{"x": 0, "y": 0, "w": 10, "h": 10, "vis": True},
                    {"x": 0, "y": 0, "w": 300, "h": 76, "vis": True}]))
    assert big["w"] == 300
    # 无候选时取 iframe
    big2 = best_container(_clear_evidence(
        iframes=[{"x": 0, "y": 0, "w": 400, "h": 76, "vis": True, "src": "hcap"}]))
    assert big2["w"] == 400
    assert best_container(_clear_evidence()) is None


def test_rule_classify_mapping():
    assert rule_classify(_clear_evidence()).present is False
    assert rule_classify(_clear_evidence(text_hit=True)).present is False  # 纯弱命中不判
    assert rule_classify(_slider_ev()).type == "slider"
    assert rule_classify(_iframe_ev()).type == "checkbox"
    points = _clear_evidence(text="请点击所有包含猫的图片", text_hit=True)
    assert rule_classify(points).type == "points_click"
    ocr = _clear_evidence(text="请输入右侧验证码", ocr_hint=True)
    assert rule_classify(ocr).type == "ocr"
    geetest = _clear_evidence(
        iframes=[{"x": 0, "y": 0, "w": 300, "h": 200, "vis": True, "src": "https://gt.geetest.com"}])
    assert rule_classify(geetest).type == "slider"


def test_parse_model_output_robust():
    assert parse_model_output(None) is None
    assert parse_model_output("hello") is None
    assert parse_model_output('```json\n{"present": true, "type": "slider"}\n```') == {
        "present": True, "type": "slider"}
    assert parse_model_output('前缀 {"a": 1} 后缀') == {"a": 1}


def test_result_from_model_fusion(cfg):
    ev = _slider_ev()
    fallback = rule_classify(ev)
    r = result_from_model(
        {"present": False, "type": "none", "confidence": 0.9, "reason": "无"},
        ev, fallback)
    # 模型判无但规则强命中 -> 规则兜底
    assert r.present is True and r.type == "slider"
    r2 = result_from_model(
        {"present": True, "type": "points_click", "confidence": 0.8,
         "reason": "看到网格", "target": "bus", "grid": {"rows": 3, "cols": 3}},
        ev, fallback)
    assert r2.type == "points_click" and r2.target == "bus"
    assert result_from_model(None, _clear_evidence(), None).present is False


def test_classifier_channels(cfg):
    # 有注入模型 -> 走模型
    clf = make_classifier(cfg, '{"present": true, "type": "slider", "confidence": 0.9}')
    assert asyncio.run(clf.classify(_slider_ev())).type == "slider"
    # 模型报错 -> 规则兜底
    async def boom(evidence_text, image):
        raise RuntimeError("model down")
    clf2 = Classifier(cfg=cfg, classify_fn=boom)
    r = asyncio.run(clf2.classify(_slider_ev()))
    assert r.present and r.type == "slider"
    # 无模型无 key -> 纯规则
    clf3 = build_classifier(cfg)
    assert asyncio.run(clf3.classify(_slider_ev())).present is True
    assert asyncio.run(clf3.classify(_clear_evidence())).present is False
    # rule_fallback 关闭且无模型 -> 弱命中不判
    clf4 = Classifier(cfg=cfg, classify_fn=None, rule_fallback=False)
    assert asyncio.run(clf4.classify(_clear_evidence(text_hit=True))).present is False


# --------------------------------------------------------------------------- trajectory / ops helpers


def test_humanize_trajectory():
    pts = humanize_trajectory(10, 20, 300, 20, steps=40, seed=7)
    assert len(pts) == 40
    assert pts[-1] == (300, 20)
    assert all(isinstance(x, float) for x, y in pts[:5])
    small = humanize_trajectory(0, 0, 1, 1, steps=2)
    assert small[-1] == (1, 1)
    # 确定性: 同种子一致
    assert humanize_trajectory(0, 0, 100, 5, seed=1) == humanize_trajectory(
        0, 0, 100, 5, seed=1)


def test_to_data_uri():
    assert to_data_uri(b"\x89PNG") == "data:image/png;base64,iVBORw=="


# --------------------------------------------------------------------------- coordinator


@pytest.mark.asyncio
async def test_coordinator_clear_is_pass(cfg):
    ops = RecordingOps(lambda expr: _clear_evidence())
    coord = VerificationCoordinator(ops, build_classifier(cfg))
    res = await coord.resolve(attempts=3)
    assert res["ok"] is True and res["attempts"] == 0


@pytest.mark.asyncio
async def test_coordinator_solves_slider(cfg):
    ops = _slider_ops()
    coord = VerificationCoordinator(ops, build_classifier(cfg))  # 规则强命中 slider
    res = await coord.resolve(attempts=3)
    assert res["ok"] is True
    assert ops.drags and len(ops.drags) >= 1
    x0, y0, x1, y1 = ops.drags[0]
    assert x1 > x0  # 拖向右侧


@pytest.mark.asyncio
async def test_coordinator_exhausted(cfg):
    # 证据一直是 checkbox 且点击无法清除 -> attempts 耗尽
    ops = RecordingOps(lambda expr: _iframe_ev())
    coord = VerificationCoordinator(
        ops, make_classifier(cfg, '{"present": true, "type": "checkbox", "confidence": 0.9}')
    )
    res = await coord.resolve(attempts=2)
    assert res["ok"] is False and res["attempts"] == 2
    assert ops.clicks  # 至少点过一次


@pytest.mark.asyncio
async def test_coordinator_ensure_clear_raises(cfg):
    ops = RecordingOps(lambda expr: _iframe_ev())
    coord = VerificationCoordinator(
        ops, make_classifier(cfg, '{"present": true, "type": "checkbox", "confidence": 0.9}')
    )
    with pytest.raises(VerificationFailed) as ei:
        await coord.ensure_clear(attempts=1, runtime_exit=True)
    assert "1 次内未通过" in str(ei.value)
    # runtime_exit=False -> 返回结果不抛
    ops2 = RecordingOps(lambda expr: _iframe_ev())
    coord2 = VerificationCoordinator(
        ops2, make_classifier(cfg, '{"present": true, "type": "checkbox", "confidence": 0.9}')
    )
    res = await coord2.ensure_clear(attempts=1, runtime_exit=False)
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_should_engage(cfg):
    assert should_engage(_clear_evidence(text_hit=True)) is False  # 弱命中不升级
    assert should_engage(_slider_ev()) is True
    assert should_engage(_iframe_ev()) is True


# --------------------------------------------------------------------------- scope(产物运行期)


class FakePage:
    """极小 page: evaluate 返回可变证据; mouse 记录; click 清掉验证。"""

    def __init__(self):
        self._evidence = _clear_evidence()
        self.clicks = 0

    async def evaluate(self, expression, *args, **kwargs):
        return self._evidence

    def set_evidence(self, ev: dict) -> None:
        self._evidence = ev

    async def screenshot(self, **kwargs):
        return b"shot"

    @property
    def mouse(self):
        return FakeMouse(self)


class FakeMouse:
    def __init__(self, page: "FakePage"):
        self.page = page
        self.moves = []
        self.down_ = False

    async def move(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    async def down(self) -> None:
        self.down_ = True

    async def up(self) -> None:
        self.down_ = False
        self.page.clicks += 1
        # 模拟勾选成功 -> 挑战消失
        self.page.set_evidence(_clear_evidence())


class FakeStickyPage(FakePage):
    """点击无法清除验证(用于限次失败的场景)。"""

    @property
    def mouse(self):
        return FakeStickyMouse(self)


class FakeStickyMouse(FakeMouse):
    async def up(self) -> None:
        self.down_ = False
        self.page.clicks += 1
        # 挑战保持原样, 模拟自动解始终失败
        self.page.set_evidence(_iframe_ev())


@pytest.mark.asyncio
async def test_scope_no_verification_pass(cfg):
    page = FakePage()
    async with VerifyScope(cfg, page, attempts=2, wait_on_exit=True) as vc:
        assert await vc.passed() is True
        await asyncio.sleep(0.05)
    assert vc.status()["failure"] is None
    assert vc.status()["episodes"] >= 0


@pytest.mark.asyncio
async def test_scope_solves_then_clear(cfg):
    page = FakePage()
    page.set_evidence(_iframe_ev())
    clf = make_classifier(cfg, '{"present": true, "type": "checkbox", "confidence": 0.9}')
    async with VerifyScope(cfg, page, attempts=3, wait_on_exit=True, classifier=clf) as vc:
        # watcher 在后台自动解; 显式等绿灯
        assert await vc.passed() is True
    assert page.clicks >= 1
    assert vc.status()["failure"] is None


@pytest.mark.asyncio
async def test_scope_failure_raises_on_exit(cfg):
    page = FakeStickyPage()
    page.set_evidence(_iframe_ev())
    clf = make_classifier(cfg, '{"present": true, "type": "checkbox", "confidence": 0.9}')
    with pytest.raises(VerificationFailed) as ei:
        async with VerifyScope(cfg, page, attempts=1, wait_on_exit=True, classifier=clf):
            await asyncio.sleep(0.05)
    assert "1 次内未通过" in str(ei.value)
    assert page.clicks >= 1


@pytest.mark.asyncio
async def test_scope_debug_mode_no_raise(cfg):
    dbg_cfg = Config(verify_runtime_exit=False)
    page = FakeStickyPage()
    page.set_evidence(_iframe_ev())
    clf = make_classifier(dbg_cfg, '{"present": true, "type": "checkbox", "confidence": 0.9}')
    async with VerifyScope(dbg_cfg, page, attempts=1, wait_on_exit=True, classifier=clf) as vc:
        await asyncio.sleep(0.05)
    assert vc.status()["failure"] is not None


@pytest.mark.asyncio
async def test_scope_wait_on_exit_false(cfg):
    page = FakePage()
    page.set_evidence(_iframe_ev())
    clf = make_classifier(cfg, '{"present": true, "type": "checkbox", "confidence": 0.9}')
    async with VerifyScope(cfg, page, attempts=2, wait_on_exit=False, classifier=clf):
        await asyncio.sleep(0.08)  # 等 watcher 自动解发生
    assert page.clicks >= 1


@pytest.mark.asyncio
async def test_scope_exception_in_body_not_masked(cfg):
    page = FakePage()
    page.set_evidence(_iframe_ev())
    clf = make_classifier(cfg, '{"present": true, "type": "checkbox", "confidence": 0.9}')
    with pytest.raises(RuntimeError):
        async with VerifyScope(cfg, page, attempts=1, wait_on_exit=True, classifier=clf):
            raise RuntimeError("boom")


# --------------------------------------------------------------------------- ops 执行后端


class _OpsPage:
    """PlaywrightOps 用的极简 page: evaluate 返回/可抛错; mouse 记录; screenshot 记录。"""

    def __init__(self, value=None, raise_eval=False):
        self.value = value
        self.raise_eval = raise_eval
        self.moves: list[tuple] = []
        self.downs = 0
        self.ups = 0
        self.screenshot_kwargs = None

    async def evaluate(self, expression, *args, **kwargs):
        if self.raise_eval:
            raise RuntimeError("eval boom")
        return self.value

    @property
    def mouse(self):
        return self

    async def move(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    async def down(self) -> None:
        self.downs += 1

    async def up(self) -> None:
        self.ups += 1

    async def screenshot(self, **kwargs):
        self.screenshot_kwargs = kwargs
        return b"png"


@pytest.mark.asyncio
async def test_playwright_ops():
    from backend.services.agent.verify.ops import PlaywrightOps

    page = _OpsPage(value={"ok": 1})
    ops = PlaywrightOps(page)
    assert await ops.evaluate("x", timeout=5.0) == {"ok": 1}
    assert await ops.evaluate("x", timeout=0.0001) is not None  # 非空 fast
    # 求值异常 -> None
    bad = _OpsPage(raise_eval=True)
    ops2 = PlaywrightOps(bad)
    assert await ops2.evaluate("x") is None
    # 截图带区域
    shot = await ops.screenshot({"x": 1, "y": 2, "w": 30, "h": 10})
    assert shot == b"png"

    class _BadShot:
        async def screenshot(self, **kwargs):
            raise RuntimeError("shot boom")

    assert await PlaywrightOps(_BadShot()).screenshot() == b""
    # 点击与拖动
    await ops.coords_click(10, 20)
    assert page.downs == 1 and page.ups == 1 and page.moves[-1] == (10, 20)
    await ops.coords_drag(0, 0, 100, 5, duration=0.1)
    assert page.downs == 2 and page.ups == 2 and page.moves[-1] == (100, 5)


@pytest.mark.asyncio
async def test_bridge_ops():
    from backend.services.agent.verify.ops import BridgeOps

    calls: dict = {}

    class Br:
        async def evaluate(self, expression, timeout=5.0):
            calls.setdefault("evaluate", []).append(expression)
            return {"ok": True, "item": {"v": "42"}}

    b = BridgeOps(Br())
    assert await b.evaluate("x") == "42"

    class BrBad(Br):
        async def evaluate(self, expression, timeout=5.0):
            return {"ok": False, "error": "no"}

    assert await BridgeOps(BrBad()).evaluate("x") is None

    class BrBoom(Br):
        async def evaluate(self, expression, timeout=5.0):
            raise RuntimeError("down")

    assert await BridgeOps(BrBoom()).evaluate("x") is None
    # 无 screenshot / coords 方法 -> 安全 no-op
    ops = BridgeOps(Br())
    assert await ops.screenshot() == b""
    await ops.coords_click(1, 2)
    await ops.coords_drag(0, 0, 5, 5)
    # 全能力桥
    class Full:
        def __init__(self):
            self.clicks = []
            self.drags = []

        async def screenshot(self):
            return b"full-shot"

        async def coords_click(self, x, y):
            self.clicks.append((x, y))

        async def coords_drag(self, x0, y0, x1, y1, duration=0.6):
            self.drags.append((x0, y0, x1, y1))

    full = Full()
    ops2 = BridgeOps(full)
    assert await ops2.screenshot() == b"full-shot"
    await ops2.coords_click(1, 2)
    await ops2.coords_drag(0, 0, 9, 9)
    assert full.clicks == [(1, 2)] and full.drags == [(0, 0, 9, 9)]


# --------------------------------------------------------------------------- 求解器


class _Res:
    def __init__(self, container=None, target="", grid=None):
        self.container = container
        self.target = target
        self.grid = grid


@pytest.mark.asyncio
async def test_solvers_checkbox():
    from backend.services.agent.verify import solvers

    rect = {"x": 100, "y": 100, "w": 300, "h": 76}
    ops = RecordingOps(lambda expr: _clear_evidence())
    assert await solvers.solve_checkbox(ops, _Res(container=rect), {}) is True
    assert ops.clicks
    x, y = ops.clicks[0]
    assert abs(x - (100 + 45)) < 0.001
    # 无容器且证据无矩形 -> False
    ops2 = RecordingOps(lambda expr: _clear_evidence())
    assert await solvers.solve_checkbox(ops2, _Res(container=None), _clear_evidence()) is False


@pytest.mark.asyncio
async def test_solvers_slider_plain():
    from backend.services.agent.verify import solvers

    rect = {"x": 200, "y": 300, "w": 300, "h": 40}
    ops = RecordingOps(lambda expr: _clear_evidence())
    assert await solvers.solve_slider(ops, _Res(container=rect), {}) is True
    x0, y0, x1, y1 = ops.drags[0]
    assert x1 > x0 and abs(y1 - y0) < 0.001
    # 无容器 -> False
    ops2 = RecordingOps(lambda expr: _clear_evidence())
    assert await solvers.solve_slider(ops2, _Res(container=None), _clear_evidence()) is False


@pytest.mark.asyncio
async def test_solvers_slider_gap():
    from backend.services.agent.verify import solvers

    rect = {"x": 100, "y": 200, "w": 300, "h": 40}
    ops = RecordingOps(lambda expr: _clear_evidence())
    assert await solvers.solve_slider_gap(ops, _Res(container=rect), {}, gap_x=80) is True
    x0, y0, x1, y1 = ops.drags[0]
    assert abs(x1 - (x0 + 80)) < 0.001
    # gap_x 缺失 -> False(不拖)
    ops2 = RecordingOps(lambda expr: _clear_evidence())
    assert await solvers.solve_slider_gap(ops2, _Res(container=rect), {}, gap_x=None) is False
    assert not ops2.drags
    # 无容器 -> False
    assert await solvers.solve_slider_gap(
        RecordingOps(lambda expr: _clear_evidence()), _Res(container=None), {}) is False


@pytest.mark.asyncio
async def test_solvers_points():
    from backend.services.agent.verify import solvers

    rect = {"x": 0, "y": 0, "w": 300, "h": 300}
    ops = RecordingOps(lambda expr: _clear_evidence())
    assert await solvers.solve_points(ops, _Res(container=rect, grid={"rows": 3, "cols": 3}),
                                      {}, cells=[0, 4, 8], grid_rows=3, grid_cols=3) is True
    assert len(ops.clicks) == 3
    # 无 cells -> False
    ops2 = RecordingOps(lambda expr: _clear_evidence())
    assert await solvers.solve_points(ops2, _Res(container=rect), {}, cells=[],
                                      grid_rows=3, grid_cols=3) is False
    assert not ops2.clicks
    # 无容器 -> False
    assert await solvers.solve_points(RecordingOps(lambda expr: _clear_evidence()),
                                      _Res(container=None), {}, cells=[0]) is False
