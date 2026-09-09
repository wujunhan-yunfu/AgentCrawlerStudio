"""人机验证: 协调器(证据→判定→求解→复核)。

产物运行期语义(见 docs/human-verification-solution.md §7.2):
- 单次"验证触发"按 attempts 计预算, 每次尝试后重新证据+判定;
- 预算耗尽仍被拦截 → 抛 VerificationFailed(由 run_code 顶层捕获结束本次运行);
- 从不弹窗、不依赖任何人工 gate。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .classify import (
    TYPE_CHECKBOX,
    TYPE_OCR,
    TYPE_POINTS,
    TYPE_SLIDER,
    Classifier,
    ClassifyResult,
    build_classifier,
    rule_classify,
)
from .evidence import EVIDENCE_JS, best_container, parse_evidence
from .exceptions import VerificationFailed
from .ops import PageOps
from . import solvers

log = logging.getLogger(__name__)


def should_engage(evidence: dict[str, Any]) -> bool:
    """是否升级到"判定/求解"。

    避免弱线索(如普通输入框 placeholder 含"验证码")触发每秒一次的模型判定:
    只有 可见验证 iframe / 可见候选交互元素 / 规则强命中 之一出现才升级。
    """
    ev = parse_evidence(evidence)
    if any(f.get("vis") for f in ev.get("iframes") or []):
        return True
    if any(c.get("vis") for c in ev.get("candidates") or []):
        return True
    return rule_classify(ev).present


class VerificationCoordinator:
    """给定 ops + classifier, 完成单次"证据→判定→求解"闭环。"""

    def __init__(
        self,
        ops: PageOps,
        classifier: Classifier,
        *,
        slider_only_right: bool = True,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        point_fn: Callable[[bytes, dict[str, Any]], Awaitable[list[int]]] | None = None,
    ):
        self.ops = ops
        self.classifier = classifier
        self.slider_only_right = slider_only_right
        self.on_event = on_event
        self.point_fn = point_fn

    async def collect_evidence(self) -> dict[str, Any]:
        raw = await self.ops.evaluate(EVIDENCE_JS, timeout=5.0)
        return parse_evidence(raw)

    async def judge(self, evidence: dict[str, Any],
                    image: bytes | None = None) -> ClassifyResult:
        return await self.classifier.classify(evidence, image=image)

    async def _maybe_shot(self, evidence: dict[str, Any]) -> bytes | None:
        """仅当配置了视觉模型时, 对"最像验证区域"截图供模型看图。"""
        if not hasattr(self.classifier, "_vision_configured") or not self.classifier._vision_configured():
            return None
        rect = best_container(evidence)
        if not rect:
            return None
        try:
            return await self.ops.screenshot(
                {"x": rect.get("x", 0), "y": rect.get("y", 0),
                 "w": rect.get("w", 0), "h": rect.get("h", 0)}
            )
        except Exception:  # noqa: BLE001
            return None

    async def _dispatch(self, res: ClassifyResult, evidence: dict[str, Any]) -> bool:
        """执行一次类型对应动作; 返回动作是否产生(否=该类型不支持自动解)。"""
        if res.type == TYPE_CHECKBOX:
            return await solvers.solve_checkbox(self.ops, res, evidence)
        if res.type == TYPE_SLIDER:
            return await solvers.solve_slider(self.ops, res, evidence)
        if res.type == TYPE_POINTS:
            image = await self._maybe_shot(evidence)
            cells: list[int] = []
            rows, cols = 3, 3
            if res.grid:
                rows = int(res.grid.get("rows") or rows)
                cols = int(res.grid.get("cols") or cols)
            if self.point_fn is not None and image:
                try:
                    cells = await self.point_fn(image, {"rows": rows, "cols": cols,
                                                        "target": res.target})
                except Exception:  # noqa: BLE001
                    cells = []
            return await solvers.solve_points(
                self.ops, res, evidence, cells=cells,
                grid_rows=rows, grid_cols=cols, target=res.target,
            )
        return False

    async def resolve(self, attempts: int) -> dict[str, Any]:
        """反复尝试直到放行或预算耗尽(不抛异常)。

        Returns: {"ok": bool, "attempts": int, "type": str, "reason": str}
        """
        used = 0
        last_type = ""
        last_reason = ""
        while used < attempts:
            ev = await self.collect_evidence()
            if not should_engage(ev):
                # 无线索可升级 = 无验证
                return {"ok": True, "attempts": used, "type": "", "reason": "clear"}
            image = await self._maybe_shot(ev)
            res = await self.judge(ev, image=image)
            last_type = res.type
            last_reason = res.reason
            if not res.present:
                return {"ok": True, "attempts": used, "type": last_type, "reason": last_reason}
            used += 1
            if self.on_event:
                try:
                    self.on_event({"kind": "verify_auto", "attempt": used,
                                   "type": res.type, "reason": res.reason})
                except Exception:  # noqa: BLE001
                    pass
            acted = await self._dispatch(res, ev)
            await asyncio.sleep(0.5 if acted else 0.2)
        # 预算耗尽后再复核一次(仍拦截才算失败)
        ev = await self.collect_evidence()
        if not should_engage(ev):
            return {"ok": True, "attempts": used, "type": last_type, "reason": "clear"}
        res = await self.judge(ev, image=None)
        if not res.present:
            return {"ok": True, "attempts": used, "type": res.type, "reason": res.reason}
        return {"ok": False, "attempts": used, "type": res.type, "reason": res.reason}

    async def ensure_clear(self, attempts: int, *, runtime_exit: bool = True,
                           exit_on_fail: bool = True) -> dict[str, Any]:
        """出口屏障: 复核并尽量解掉; 失败按策略抛 VerificationFailed 或返回结果。"""
        result = await self.resolve(attempts=attempts)
        if result["ok"]:
            return result
        msg = (
            f"人机验证在 {result['attempts']} 次内未通过"
            f"({result.get('type') or 'unknown'}): {result.get('reason') or '仍在拦截'}"
        )
        if runtime_exit and exit_on_fail:
            raise VerificationFailed(msg, type=result.get("type") or "",
                                     attempts=result.get("attempts") or 0,
                                     reason=result.get("reason") or "")
        return result


def build_coordinator(
    cfg: Any,
    ops: PageOps,
    *,
    classifier: Classifier | None = None,
    point_fn: Callable[[bytes, dict[str, Any]], Awaitable[list[int]]] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> VerificationCoordinator:
    clf = classifier or build_classifier(cfg)
    return VerificationCoordinator(
        ops,
        clf,
        slider_only_right=bool(cfg and cfg.verify_slider_only_right),
        point_fn=point_fn,
        on_event=on_event,
    )
