"""人机验证: 求解器(纯动作, 不做类型判断)。

只接收 §classify 判出的 type 与容器矩形执行对应动作; 动作后由上层重新
采集证据+判定确认 present=false(成功)。滑块不区分纯拖/缺口: 默认拖到轨道右端;
配置 verify_slider_only_right=False 时先用缺口估计(缺失则回退右端)。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from .evidence import best_container
from .ops import PageOps

_HANDLE_W = 24.0  # 无显式 handle 时的滑块宽估算


async def solve_checkbox(ops: PageOps, res: Any, evidence: dict[str, Any]) -> bool:
    """点击"我是真人"勾选(常用 hCaptcha iframe 左上比例点)。"""
    rect = (res.container or {}) if isinstance(res, dict) else None
    if not rect:
        if hasattr(res, "container") and res.container:
            rect = res.container
    if not rect:
        rect = best_container(evidence)
    if not rect or not rect.get("vis", True):
        return False
    x = rect.get("x", 0) + (rect.get("w", 0) or 0) * 0.15
    y = rect.get("y", 0) + (rect.get("h", 0) or 0) * 0.5
    await ops.coords_click(x, y)
    await asyncio.sleep(1.2)
    return True


async def _slider_rect(ops: PageOps, res: Any, evidence: dict[str, Any]) -> dict | None:
    rect = res.container if hasattr(res, "container") else None
    if not rect:
        rect = best_container(evidence)
    return rect


async def solve_slider_plain(ops: PageOps, res: Any, evidence: dict[str, Any]) -> bool:
    """纯滑块/无缺口: 从 handle 起点平滑拖到轨道右端。"""
    rect = await _slider_rect(ops, res, evidence)
    if not rect:
        return False
    cx = rect.get("x", 0)
    cy = rect.get("y", 0) + (rect.get("h", 0) or 0) / 2
    cw = rect.get("w", 0) or 0
    x0 = cx + _HANDLE_W / 2
    x1 = cx + max(cw - _HANDLE_W / 2 - 2, x0 + 10)
    await ops.coords_drag(x0, cy, x1, cy, duration=0.7)
    await asyncio.sleep(1.0)
    return True


async def solve_slider_gap(ops: PageOps, res: Any, evidence: dict[str, Any],
                           gap_x: float | None = None) -> bool:
    """缺口滑块: 需要缺口横向偏移 gap_x(由视觉/外部估计给出)。"""
    rect = await _slider_rect(ops, res, evidence)
    if not rect:
        return False
    if gap_x is None or not math.isfinite(float(gap_x)):
        return False
    cy = rect.get("y", 0) + (rect.get("h", 0) or 0) / 2
    x0 = rect.get("x", 0) + _HANDLE_W / 2
    x1 = x0 + float(gap_x)
    await ops.coords_drag(x0, cy, x1, cy, duration=0.8)
    await asyncio.sleep(1.2)
    return True


async def solve_slider(ops: PageOps, res: Any, evidence: dict[str, Any],
                       gap_x: float | None = None) -> bool:
    """滑块总入口: 配置 verify_slider_only_right=True 走右端纯拖, 否则尝试缺口再回退。"""
    if gap_x is not None:
        ok = await solve_slider_gap(ops, res, evidence, gap_x=gap_x)
        if ok:
            return True
    return await solve_slider_plain(ops, res, evidence)


async def solve_points(ops: PageOps, res: Any, evidence: dict[str, Any],
                       cells: list[int] | None = None,
                       grid_rows: int = 3, grid_cols: int = 3,
                       target: str = "") -> bool:
    """多图点选: 按网格格序号逐个点击格中心(cells 来自视觉/外部判定)。

    cells 为空且无视觉目标时返回 False(上层按"无法自动解"处理)。
    """
    rect = res.container if hasattr(res, "container") else None
    if not rect:
        rect = best_container(evidence)
    if not rect:
        return False
    if not cells:
        return False
    gw = rect.get("w", 0) or 0
    gh = rect.get("h", 0) or 0
    if not gw or not gh:
        return False
    rw = gw / grid_cols
    rh = gh / grid_rows
    for idx in cells:
        r, c = divmod(int(idx), grid_cols)
        x = rect.get("x", 0) + (c + 0.5) * rw
        y = rect.get("y", 0) + (r + 0.5) * rh
        await ops.coords_click(x, y)
        await asyncio.sleep(0.35)
    await asyncio.sleep(0.6)
    return True
