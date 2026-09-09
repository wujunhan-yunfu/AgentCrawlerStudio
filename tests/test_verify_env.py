"""Agent 开发期工具(tools/verify) 与 CrawlerEnv.run_code 集成测试。

使用假 bridge / 假 env, 不依赖真实 Chrome/LLM。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.config import Config


# --------------------------------------------------------------------------- tools/verify


class FakeVerifyBridge:
    """BrowserBridge 假对象: evaluate 返回证据 JSON(字符串, 同 cdp evaluate 语义)。"""

    def __init__(self, cfg: Config | None = None, evidence: dict | None = None):
        self.stream_cfg = cfg or Config()
        self.stream = type("S", (), {"cfg": self.stream_cfg})()
        self.evidence = evidence or _clear()
        self.result = {"ok": True, "item": {"v": json.dumps(self.evidence, ensure_ascii=False)}}

    async def evaluate(self, expression: str, timeout: float = 5.0):
        return self.result


def _clear() -> dict:
    return {"text": "page", "text_hit": False, "iframes": [], "candidates": [],
            "ocr_hint": False, "url": "https://example.com", "viewport": [1280, 800]}


def _slider() -> dict:
    return {"text": "拖动滑块完成拼图", "text_hit": True,
            "candidates": [{"x": 10, "y": 10, "w": 300, "h": 40, "vis": True}],
            "ocr_hint": False, "url": "https://example.com", "viewport": [1280, 800]}


@pytest.mark.asyncio
async def test_detect_none():
    from backend.services.agent.tools.verify import build_verify_tools

    bridge = FakeVerifyBridge()
    tools = {t.name: t for t in build_verify_tools(bridge)}
    out = await tools["detect_verification"].ainvoke({})
    assert "未检测到" in out


@pytest.mark.asyncio
async def test_detect_slider():
    from backend.services.agent.tools.verify import build_verify_tools

    bridge = FakeVerifyBridge(evidence=_slider())
    tools = {t.name: t for t in build_verify_tools(bridge)}
    out = await tools["detect_verification"].ainvoke({})
    assert "slider" in out or "滑块" in out


@pytest.mark.asyncio
async def test_solve_and_status(cfg):
    from backend.services.agent.tools.verify import build_verify_tools

    bridge = FakeVerifyBridge(cfg=cfg, evidence=_slider())
    tools = {t.name: t for t in build_verify_tools(bridge)}
    out = await tools["solve_verification"].ainvoke({"max_attempts": 1})
    # 规则识别为滑块但 bridge 无法清除 -> 自动解未通过(不会抛/不会挂起)
    assert "自动解未通过" in out or "已通过" in out
    st = await tools["verify_status"].ainvoke({})
    assert "verify_max_attempts" in st


@pytest.mark.asyncio
async def test_detect_error_handled():
    from backend.services.agent.tools.verify import build_verify_tools

    class BoomBridge(FakeVerifyBridge):
        async def evaluate(self, expression: str, timeout: float = 5.0):
            raise RuntimeError("no page")

    tools = {t.name: t for t in build_verify_tools(BoomBridge())}
    out = await tools["detect_verification"].ainvoke({})
    assert "检测失败" in out


# --------------------------------------------------------------------------- CrawlerEnv.verify_check


class _EvPage:
    """run_code 集成用的极简 page: verify_check 作用域在其上运行。"""

    def __init__(self):
        self._evidence = _clear()
        self.clicks = 0

    async def evaluate(self, expression, *args, **kwargs):
        return self._evidence

    def set_evidence(self, ev: dict) -> None:
        self._evidence = ev

    @property
    def mouse(self):
        return self._mouse if hasattr(self, "_mouse") else _Mouse(self)

    async def screenshot(self, **kwargs):
        return b"shot"


class _Mouse:
    def __init__(self, page):
        self.page = page

    async def move(self, x, y):
        pass

    async def down(self):
        pass

    async def up(self):
        self.page.clicks += 1
        self.page.set_evidence(_clear())


@pytest.mark.asyncio
async def test_crawler_env_verify_check_pass(cfg):
    from backend.services.agent.verify.scope import VerifyScope  # noqa: F401
    from backend.services.crawler import CrawlerEnv

    page = _EvPage()
    env = CrawlerEnv(cfg, page)
    ran = []
    async with env.verify_check(timeout=0, attempts=3) as vc:
        ran.append(vc.status())
    assert len(ran) == 1


@pytest.mark.asyncio
async def test_crawler_env_verify_check_solves(cfg):
    from backend.services.crawler import CrawlerEnv

    page = _EvPage()
    page.set_evidence(_slider())
    env = CrawlerEnv(cfg, page)
    async with env.verify_check(timeout=0, attempts=2, wait_on_exit=True):
        pass
    # 规则识别滑块 -> 拖拽结束(up 清掉验证) -> 通过, 无 VerificationFailed
    assert page.clicks >= 1
