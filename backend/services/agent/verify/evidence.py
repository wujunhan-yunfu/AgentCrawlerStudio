"""人机验证: 证据采集 JS 与解析。

证据层只产出"结构化线索"(文本/iframe 外壳/候选元素矩形),不判型。
类型判定由 classify.py 的大模型(规则作辅助/回退)完成,见 docs/human-verification-solution.md §3。
"""

from __future__ import annotations

import json
from typing import Any

# 顶层 document 扫描: 只给线索与坐标环境, 不做厂商/类型断言
EVIDENCE_JS = r"""
(() => {
  const visRect = el => {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height),
            vis: r.width > 0 && r.height > 0 && el.offsetParent !== null};
  };
  // 1) 顶层可见文本(末尾 500 字足够覆盖风控提示)
  const txt = (document.body ? document.body.innerText : '') || '';
  const re = /(滑块|拖动滑块|滑动验证|向右拖动|完成验证|我是真人|我不是机器人|安全验证|请完成验证|点击验证|verify|security check|slider|drag to|I'?m (not )?a robot|please click|captcha|验证码)/i;
  // 2) iframe 外壳矩形(验证码常见 src/class; 跨域内部 DOM 不可见但外壳可拿)
  const iframes = Array.from(document.querySelectorAll('iframe')).filter(f => {
    const s = (f.getAttribute('src') || '') + ' ' + (f.getAttribute('class') || '');
    return /(captcha|verify|geetest|hcaptcha|recaptcha|tcaptcha|fc\.crisp|turnstile|若依|ajax\.ai)/i.test(s);
  }).slice(0, 4).map(f => ({ ...visRect(f), src: (f.getAttribute('src') || '').slice(0, 120) }));
  // 3) 顶层候选交互元素(滑块/拼图/点选网格常见 class)
  const cands = Array.from(document.querySelectorAll(
    '[class*="slider"],[class*="geetest"],[id*="slide"],[class*="captcha"],[class*="verify"],' +
    '[class*="check"],[class*="yidun"],[class*="tc-action"],[class*="pp-captcha"],' +
    'img[src*="captcha"],canvas'))
    .map(visRect).filter(e => e.vis).slice(0, 8);
  // 4) 纯 OCR 图形验证码线索(账号注册类, 走既有登录流程, 不在本模块自动解)
  const ocr = /(输入|填写).{0,6}(验证码|code)/i.test(txt);
  return {
    text: txt.slice(-500),
    text_hit: re.test(txt),
    iframes: iframes.filter(i => i.vis),
    candidates: cands,
    ocr_hint: ocr,
    url: (location.href || '').slice(0, 200),
    viewport: [window.innerWidth, window.innerHeight],
  };
})()
"""


def parse_evidence(raw: Any) -> dict[str, Any]:
    """把 evaluate 返回的证据统一成 dict(容错字符串/None)。"""
    if isinstance(raw, dict):
        ev = raw
    elif isinstance(raw, str):
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            ev = {}
    else:
        ev = {}
    ev.setdefault("text", "")
    ev.setdefault("text_hit", False)
    ev.setdefault("iframes", [])
    ev.setdefault("candidates", [])
    ev.setdefault("ocr_hint", False)
    ev.setdefault("url", "")
    ev.setdefault("viewport", [0, 0])
    return ev


def has_clues(ev: dict[str, Any]) -> bool:
    """证据是否有任何可疑线索(决定是否升级到模型判定/求解)。"""
    ev = parse_evidence(ev)
    if ev.get("text_hit"):
        return True
    if any(f.get("vis") for f in ev.get("iframes") or []):
        return True
    return bool(ev.get("candidates") or [])


def best_container(ev: dict[str, Any]) -> dict[str, Any] | None:
    """从证据里挑一个"最像验证区域"的矩形(候选元素宽高最大者优先, 其次 iframe)。"""
    ev = parse_evidence(ev)
    cands = [c for c in ev.get("candidates") or [] if c.get("vis")]
    if cands:
        return max(cands, key=lambda c: (c.get("w") or 0) * (c.get("h") or 0))
    frames = [f for f in ev.get("iframes") or [] if f.get("vis")]
    if frames:
        return max(frames, key=lambda c: (c.get("w") or 0) * (c.get("h") or 0))
    return None
