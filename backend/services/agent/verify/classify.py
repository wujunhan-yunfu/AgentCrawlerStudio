"""人机验证: 类型判定。

判定原则(见 docs/human-verification-solution.md §3.2/§3.3):
- 大模型为主: 有视觉模型(或可读文本的主模型)时看证据后输出结构化判定;
- 规则为辅助: 无模型 / 模型判不定时, 规则"强直接命中"也可作为认定依据。

输出统一为 ClassifyResult。type ∈ checkbox | slider | points_click | ocr | none。
滑块不细分 plain/gap: 由求解器结合 evidence/容器按配置决定"纯拖 vs 缺口"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .evidence import parse_evidence

TYPE_CHECKBOX = "checkbox"
TYPE_SLIDER = "slider"
TYPE_POINTS = "points_click"
TYPE_OCR = "ocr"
TYPE_NONE = "none"
SUPPORTED_TYPES = (TYPE_CHECKBOX, TYPE_SLIDER, TYPE_POINTS)

# 规则"强直接命中"词表(命中才算依据, 避免弱词误报)
_RULE_SLIDER = re.compile(
    r"(拖动滑块|滑动验证|向右滑动|向右拖动|拖动到|拼图|滑块验证|geetest|极验)", re.I
)
_RULE_CHECKBOX = re.compile(
    r"(我是真人|我不是机器人|我不是自动|I'?m (not )?a robot|请点击.*验证|验证.*点击)", re.I
)
_RULE_POINTS = re.compile(
    r"(请点击|请选择|点选|选出|请选出).{0,20}(所有|包含|含|包括).{0,12}(图片|图|项|物品)", re.I
)
_RULE_POINTS2 = re.compile(
    r"(点一下|点击所有).{0,12}(图片|图)", re.I
)
_IFRAME_VENDOR = (
    ("hcaptcha", TYPE_CHECKBOX),
    ("recaptcha", TYPE_CHECKBOX),
    ("turnstile", TYPE_CHECKBOX),
    ("geetest", TYPE_SLIDER),
    ("tcaptcha", TYPE_SLIDER),
)

_NORMALIZE_TYPE = {
    "checkbox": TYPE_CHECKBOX,
    "captcha": TYPE_CHECKBOX,
    "slider": TYPE_SLIDER,
    "slider_plain": TYPE_SLIDER,
    "slider_gap": TYPE_SLIDER,
    "puzzle": TYPE_SLIDER,
    "points_click": TYPE_POINTS,
    "image_click": TYPE_POINTS,
    "select": TYPE_POINTS,
    "ocr": TYPE_OCR,
    "none": TYPE_NONE,
    "": TYPE_NONE,
}


@dataclass
class ClassifyResult:
    present: bool = False
    type: str = TYPE_NONE
    confidence: float = 0.0
    reason: str = ""
    container: dict[str, Any] | None = None
    target: str = ""
    grid: dict[str, Any] | None = None
    model: bool = False  # True=模型判定; False=规则直接命中

    def as_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "type": self.type,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "container": self.container,
            "target": self.target,
            "grid": self.grid,
            "model": self.model,
        }


# --------------------------------------------------------------------------- 规则


def rule_classify(evidence: dict[str, Any]) -> ClassifyResult:
    """规则"强直接命中": 只在文案/特征非常明确时给出结论(弱线索返回 present=False)。"""
    ev = parse_evidence(evidence)
    text = str(ev.get("text") or "")
    frames = [f for f in ev.get("iframes") or [] if f.get("vis")]
    if ev.get("ocr_hint"):
        return ClassifyResult(
            present=True, type=TYPE_OCR, confidence=0.7,
            reason="页面提示输入图形验证码(OCR), 走既有登录流程", model=False,
        )
    # 滑块 > checkbox > 点选 的互斥判断(滑块描述最明确)
    if _RULE_SLIDER.search(text):
        container = _pick_rect(ev)
        return ClassifyResult(
            present=True, type=TYPE_SLIDER, confidence=0.85,
            reason="文案/特征命中滑块类验证", container=container, model=False,
        )
    # 点选(目标物图片)文案往往独立出现
    if _RULE_POINTS.search(text) or _RULE_POINTS2.search(text):
        return ClassifyResult(
            present=True, type=TYPE_POINTS, confidence=0.7,
            reason="文案命中图片点选类验证", container=_pick_rect(ev), model=False,
        )
    # iframe 厂商直连
    for frame in frames:
        src = str(frame.get("src") or "")
        for vendor, vtype in _IFRAME_VENDOR:
            if vendor in src.lower():
                return ClassifyResult(
                    present=True, type=vtype, confidence=0.8,
                    reason=f"识别到 {vendor} 验证 iframe", container=frame, model=False,
                )
    if _RULE_CHECKBOX.search(text) and (frames or ev.get("candidates")):
        return ClassifyResult(
            present=True, type=TYPE_CHECKBOX, confidence=0.7,
            reason="文案命中'我是真人'类验证", container=_pick_rect(ev), model=False,
        )
    return ClassifyResult(present=False, type=TYPE_NONE, confidence=0.0, reason="规则未强命中", model=False)


def _pick_rect(ev: dict[str, Any]) -> dict[str, Any] | None:
    from .evidence import best_container

    return best_container(ev)


# --------------------------------------------------------------------------- 模型

_CLASSIFY_PROMPT = """你是网页人机验证判定器。给你一段网页 DOM 证据(JSON)与可能的截图。
请判断页面是否被人机验证拦截, 以及拦截类型。只依据页面真实证据, 不要臆测。
判定优先级: 画面上真实可见的验证 UI > DOM/iframe 特征 > 页面文字提示。

证据 JSON:
{evidence}

只输出 JSON, 不要解释。字段:
{{
  "present": true/false,
  "type": "none|checkbox|slider|points_click|ocr",
  "confidence": 0.0-1.0,
  "reason": "一句话依据",
  "container": {{"x":0,"y":0,"w":0,"h":0}} 或 null,
  "target": "点选目标物描述(点选类)", 
  "grid": {{"rows":3,"cols":3}} 或 null
}}"""


def parse_model_output(raw: Any) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    # 去 markdown fence / 前后杂文本
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def result_from_model(data: dict[str, Any] | None, evidence: dict[str, Any],
                      fallback: ClassifyResult | None) -> ClassifyResult:
    """把模型 JSON 归一成 ClassifyResult; 模型判不定时用规则强命中兜底(§3.3)。"""
    ev = parse_evidence(evidence)
    if data is None or not isinstance(data.get("present"), bool):
        # 模型没给出可解析结论 -> 规则兜底
        return fallback or ClassifyResult(present=False, type=TYPE_NONE,
                                          confidence=0.0, reason="模型未返回可解析结果", model=True)
    vtype = _NORMALIZE_TYPE.get(str(data.get("type") or "").strip().lower(), TYPE_NONE)
    if not data["present"] or vtype == TYPE_NONE:
        # 模型说没有验证; 但规则强命中且模型其实没看图/只给了文本 -> 允许规则认定
        if fallback and fallback.present:
            return fallback
        return ClassifyResult(present=False, type=TYPE_NONE,
                              confidence=float(data.get("confidence") or 0.5),
                              reason=str(data.get("reason") or "模型判定无验证"), model=True)
    return ClassifyResult(
        present=True,
        type=vtype,
        confidence=float(data.get("confidence") or 0.8),
        reason=str(data.get("reason") or "模型判定命中"),
        container=data.get("container") or _pick_rect(ev),
        target=str(data.get("target") or ""),
        grid=data.get("grid"),
        model=True,
    )


class Classifier:
    """类型判定器: 模型为主(可选), 规则为辅助/回退。

    classify_fn 注入可测:  async (evidence_text: str, image: bytes|None) -> str(模型输出)
    未注入时按 cfg 自动构建(视觉模型或文本模型), 无 API Key 则退化为纯规则。
    """

    def __init__(self, cfg: Any = None,
                 classify_fn: Callable[[str, bytes | None], Awaitable[Any]] | None = None,
                 rule_fallback: bool | None = None):
        self.cfg = cfg
        self._classify_fn = classify_fn
        self._rule_fallback = rule_fallback

    def _use_rule_fallback(self) -> bool:
        if self._rule_fallback is not None:
            return self._rule_fallback
        return bool(self.cfg and self.cfg.verify_rule_fallback)

    def _vision_configured(self) -> bool:
        return bool(self.cfg and self.cfg.verify_vision_model)

    def _api_key(self) -> str:
        return (self.cfg.llm_api_key or "").strip() if self.cfg else ""

    def _text_fn(self) -> Callable[[str, bytes | None], Awaitable[Any]] | None:
        """主 Agent 模型文本判定(无图)。仅在配置了 LLM key 时可用。"""
        if not self._api_key():
            return None
        from langchain_core.messages import HumanMessage, SystemMessage

        from ..core.llm import build_chat_model

        async def _fn(evidence_text: str, image: bytes | None) -> Any:
            model = build_chat_model(self.cfg)
            resp = await model.ainvoke(
                [
                    SystemMessage(content=_CLASSIFY_PROMPT.format(evidence=evidence_text)),
                    HumanMessage(content="请判定。"),
                ],
                timeout=90,
            )
            return resp.content

        return _fn

    def _vision_fn(self) -> Callable[[str, bytes | None], Awaitable[Any]] | None:
        if not self._vision_configured():
            return None
        if not self._api_key():
            return None
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        from ..core.llm import _PROVIDER_BASE_URLS

        base = (self.cfg.verify_vision_base_url or "").strip() or _PROVIDER_BASE_URLS.get(
            (self.cfg.verify_vision_provider or "").strip() or "openai"
        )
        model = ChatOpenAI(
            model=self.cfg.verify_vision_model,
            api_key=SecretStr(self._api_key()),
            base_url=base,
            temperature=0.0,
            timeout=120,
        )

        async def _fn(evidence_text: str, image: bytes | None) -> Any:
            from .ops import to_data_uri

            content: list[Any] = [
                {"type": "text", "text": _CLASSIFY_PROMPT.format(evidence=evidence_text)}
            ]
            if image:
                content.append({"type": "image_url", "image_url": {"url": to_data_uri(image)}})
            resp = await model.ainvoke([HumanMessage(content=content)], timeout=120)
            return resp.content

        return _fn

    async def classify(self, evidence: dict[str, Any],
                       image: bytes | None = None) -> ClassifyResult:
        ev = parse_evidence(evidence)
        rule = rule_classify(ev)
        use_model = self._classify_fn is not None or self._vision_configured() or self._api_key()
        if use_model:
            fn = self._classify_fn or self._vision_fn() or self._text_fn()
        else:
            fn = None
        if fn is None:
            # 无任何模型: 纯规则(允许强命中作为依据)
            return rule if (rule.present and self._use_rule_fallback()) else ClassifyResult(
                present=False, type=TYPE_NONE, confidence=0.0,
                reason="无模型且规则未强命中", model=False,
            )
        try:
            raw = await fn(json.dumps(ev, ensure_ascii=False), image)
        except Exception as exc:  # noqa: BLE001
            rule = rule or ClassifyResult(present=False, type=TYPE_NONE, confidence=0.0, reason="")
            return result_from_model(None, ev, rule if rule.present else None)
        data = parse_model_output(raw)
        return result_from_model(data, ev, rule if rule.present else None)


def build_classifier(cfg: Any, classify_fn=None, rule_fallback: bool | None = None) -> Classifier:
    return Classifier(cfg=cfg, classify_fn=classify_fn, rule_fallback=rule_fallback)
