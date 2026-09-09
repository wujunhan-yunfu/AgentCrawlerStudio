"""Agent 开发期工具: 检测/求解人机验证(会话内; 产物运行期由 verify_check 负责)。

- detect_verification(): 采集证据并按模型(规则辅助)判定是否被验证拦截;
- solve_verification(): 运行同一套"证据→判定→自动解"链(限次), 不弹窗;
  仍失败时把结论交回 Agent, 由 Agent 决定 ask_user 询问用户或交付;
- verify_status(): 查看验证预算/最近一次自动解结论。

命名与运行期上下文管理器 verify_check(§7.2)区分: 开发期工具不包裹代码,
只是对"当前页面是否被验证拦"做一次判定/尝试。
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from ..verify.coordinator import VerificationCoordinator, should_engage
from ..verify.classify import build_classifier
from ..verify.ops import BridgeOps


def _cfg(bridge) -> None:
    stream = getattr(bridge, "stream", None)
    return getattr(stream, "cfg", None)


def _coordinator(bridge, cache: dict):
    cfg = _cfg(bridge)
    return VerificationCoordinator(
        BridgeOps(bridge),
        build_classifier(cfg),  # cfg=None 时退化为纯规则
        slider_only_right=bool(cfg and cfg.verify_slider_only_right),
    )


def build_verify_tools(bridge) -> list:
    """开发期人机验证工具(detect / solve / status)。"""
    cache: dict = {}

    @tool
    async def detect_verification() -> str:
        """检测当前页面是否被人机验证(滑块/我是真人/图片点选)拦截, 并判定类型。

        用于 navigate/run_code 后 Agent 怀疑页面卡在验证时的第一判断。
        返回: 未检测到 / 检测到并给出类型、容器坐标、依据。
        """
        try:
            coord = _coordinator(bridge, cache)
            ev = await coord.collect_evidence()
            if not ev.get("url") and not ev.get("text"):
                cache["last"] = {"present": False}
                return "检测失败: 无法读取页面证据"
            if not should_engage(ev):
                cache["last"] = {"present": False}
                return "未检测到人机验证"
            res = await coord.judge(ev, image=None)
            cache["last"] = res.as_dict()
            if not res.present:
                return "未检测到人机验证(存在可疑线索但未能确认)"
            return (
                f"检测到人机验证: 类型={res.type}, 置信={res.confidence}, "
                f"依据={res.reason}"
                + (f", 容器={res.container}" if res.container else "")
            )
        except Exception as exc:  # noqa: BLE001
            return f"检测失败: {exc}"

    @tool
    async def solve_verification(max_attempts: int | None = None) -> str:
        """尝试自动通过当前页面的标准人机验证(滑块/我是真人/图片点选), 限次自动尝试。

        不弹窗、不等待用户。成功返回"已通过"; 限次内未通过返回原因,
        此时若确有验证卡住, 你可考虑 ask_user 询问用户是否人工在实时画面完成/是否交付。
        Args:
            max_attempts: 自动尝试次数上限, 缺省取配置 verify_max_attempts。
        Returns:
            自动解结果(通过 / 未通过及原因)。
        """
        try:
            coord = _coordinator(bridge, cache)
            cfg = _cfg(bridge)
            attempts = int(max_attempts) if max_attempts else int(
                getattr(cfg, "verify_max_attempts", 3) or 3
            )
            result = await coord.ensure_clear(attempts=attempts, runtime_exit=False)
            cache["last"] = result
            if result.get("ok"):
                return "已通过人机验证"
            return (
                f"自动解未通过(尝试 {result.get('attempts', attempts)} 次): "
                f"{result.get('reason') or '仍在拦截'}"
            )
        except Exception as exc:  # noqa: BLE001
            return f"自动解异常: {exc}"

    @tool
    async def verify_status() -> str:
        """查看人机验证的自动尝试预算与最近一次判定/求解结论(排查卡住原因)。"""
        cfg = _cfg(bridge)
        last = cache.get("last")
        base = {
            "verify_enabled": bool(cfg and cfg.verify_enabled),
            "verify_max_attempts": int(getattr(cfg, "verify_max_attempts", 3) or 3),
            "vision_model": (cfg.verify_vision_model or "") if cfg else "",
        }
        if last:
            base["last"] = last
        return json.dumps(base, ensure_ascii=False)

    return [detect_verification, solve_verification, verify_status]
