"""人机验证: 执行后端(ops)抽象与坐标动作。

求解器不关心是"Agent 主循环遥控"还是"脚本内 Playwright 页面"——
统一收敛成 PageOps 接口(见 docs/human-verification-solution.md §7.5):
- PlaywrightOps: 脚本运行期直接用 playwright page(常驻, 坐标系即视口);
- BridgeOps:     Agent 主循环经 BrowserBridge 遥控(截图/求值经 bridge)。

坐标点击/拖动走 CDP 输入管线(页面鼠标), 天然命中 iframe/OOPIF 内的挑战,
无需穿透 iframe DOM。
"""

from __future__ import annotations

import asyncio
import base64
import random
from typing import Any, Protocol


class PageOps(Protocol):
    """求解器所需的最小页面操作集合(视口坐标系)。"""

    async def evaluate(self, expression: str, timeout: float = 5.0) -> Any: ...

    async def screenshot(self, region: dict[str, float] | None = None) -> bytes: ...

    async def coords_click(self, x: float, y: float) -> None: ...

    async def coords_drag(
        self, x0: float, y0: float, x1: float, y1: float, duration: float = 0.6
    ) -> None: ...


class PlaywrightOps:
    """脚本运行期执行后端: 直接用 playwright page(常驻)。"""

    def __init__(self, page: Any):
        self.page = page

    async def evaluate(self, expression: str, timeout: float = 5.0) -> Any:
        try:
            return await asyncio.wait_for(
                self.page.evaluate(expression), timeout=timeout
            )
        except Exception:  # noqa: BLE001  超时/页面异常一律按"无证据"处理
            return None

    async def screenshot(self, region: dict[str, float] | None = None) -> bytes:
        kwargs: dict[str, Any] = {"type": "png"}
        if region:
            r = region
            kwargs["clip"] = {
                "x": r.get("x", 0),
                "y": r.get("y", 0),
                "width": max(1.0, r.get("w", 0)),
                "height": max(1.0, r.get("h", 0)),
            }
        try:
            return await self.page.screenshot(**kwargs)
        except Exception:  # noqa: BLE001
            return b""

    async def coords_click(self, x: float, y: float) -> None:
        mouse = self.page.mouse
        await mouse.move(x, y)
        await mouse.down()
        await mouse.up()

    async def coords_drag(
        self, x0: float, y0: float, x1: float, y1: float, duration: float = 0.6
    ) -> None:
        mouse = self.page.mouse
        await mouse.move(x0, y0)
        await mouse.down()
        pts = humanize_trajectory(x0, y0, x1, y1, steps=36)
        total = max(duration, 0.1)
        step = total / max(len(pts), 1)
        for px, py in pts:
            await mouse.move(px, py)
            await asyncio.sleep(step * random.uniform(0.8, 1.2))
        await mouse.move(x1, y1)
        await mouse.up()


class BridgeOps:
    """Agent 主循环执行后端: 经 BrowserBridge 遥控(截图/求值)。"""

    def __init__(self, bridge: Any):
        self.bridge = bridge

    async def evaluate(self, expression: str, timeout: float = 5.0) -> Any:
        try:
            result = await self.bridge.evaluate(expression, timeout=timeout)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(result, dict) or not result.get("ok"):
            return None
        return (result.get("item") or {}).get("v")

    async def screenshot(self, region: dict[str, float] | None = None) -> bytes:
        shot = getattr(self.bridge, "screenshot", None)
        if shot is None:
            return b""
        try:
            return await shot()
        except Exception:  # noqa: BLE001
            return b""

    async def coords_click(self, x: float, y: float) -> None:
        click = getattr(self.bridge, "coords_click", None)
        if click is None:
            return
        await click(x, y)

    async def coords_drag(
        self, x0: float, y0: float, x1: float, y1: float, duration: float = 0.6
    ) -> None:
        drag = getattr(self.bridge, "coords_drag", None)
        if drag is None:
            return
        await drag(x0, y0, x1, y1, duration=duration)


# --------------------------------------------------------------------------- 轨迹


def humanize_trajectory(
    x0: float, y0: float, x1: float, y1: float, steps: int = 40, seed: int | None = None
) -> list[tuple[float, float]]:
    """生成带弧线/速度起伏/抖动的"拟人"拖动点列(视口坐标)。

    只做"像人"的轨迹节奏, 不做 webdriver 隐藏等对抗行为(合规, 见文档 §11)。
    保证终点精确落在 (x1, y1)。
    """
    rng = random.Random(seed)
    if steps < 3:
        return [(x1, y1)]
    # 二次贝塞尔: 控制点在 y 上小幅偏移制造弧线
    arc = rng.uniform(-3.0, 3.0)
    cx = (x0 + x1) / 2 + rng.uniform(-6, 6)
    cy = (y0 + y1) / 2 + arc
    pts: list[tuple[float, float]] = []
    # 速度模型: 慢(0~12%) 快(12~70%) 慢收尾(70~100%)
    for i in range(1, steps):
        t = i / steps
        eased = t * t * (3 - 2 * t)  # smoothstep: 起收慢
        # 采样点用非线性 t 制造"前段稀疏后段密集"? 反向: 使用均匀 ease 即可
        bx = (1 - eased) ** 2 * x0 + 2 * (1 - eased) * eased * cx + eased**2 * x1
        by = (1 - eased) ** 2 * y0 + 2 * (1 - eased) * eased * cy + eased**2 * y1
        jx = rng.gauss(0, 1.4)
        jy = rng.gauss(0, 1.2)
        pts.append((round(bx + jx, 1), round(by + jy, 1)))
    pts.append((x1, y1))
    return pts


def to_data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()
