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
        """检测当前页面是否被人机验证拦截, 并判定类型(滑块/我是真人/图片点选等)。

        使用时机(只在"怀疑被拦"时调用一次, 不要每步都调): 页面出现验证框/滑块/拼图/
        "我是真人"勾选框或点选网格; run_code/navigate 后文字或 iframe 出现 captcha/
        slider/geetest/hcaptcha 特征; 触发风控动作后(登录提交/翻页)页面异常卡住。

        流程位置: 这是"人机验证工具流程"第一步——先判型, 不求解。
        返回"未检测到"但页面仍明显异常时, 可等待 1~2s 后再最多调一次, 不要反复探测。
        Returns:
            未检测到 / 检测到并给出类型、置信度、容器坐标与依据。
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

        使用时机与顺序(硬性约束): 必须先 `detect_verification` 且确认命中后再调用本工具,
        不要凭空调用; 每次验证触发最多自动尝试 max_attempts 次(默认取配置, 通常 3), 不弹窗。

        结果处理:
        - "已通过": 重新 page_analyze/截图确认页面放行后, 回到原规划步骤继续执行;
        - "自动解未通过": 先降频等待数秒, 最多再调用一次; 仍失败立即停止重试(禁止硬闯),
          改走 ask_user 询问用户(请其在实时画面手动完成 / 换思路 / 交付已生成结果)。
        通过后把验证特征记入画像, 交付脚本时作为 verify_check(hints=...) 传入运行期。
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
        """查看人机验证的自动尝试预算与最近一次判定/求解结论(排查卡住原因)。

        使用时机: 仅当"反复卡在验证"或"想确认上次自动解结论/预算"时调用一次, 属排查工具,
        不作为常规步骤, 也不要频繁调用。返回后结合结论决定是继续自动解、ask_user 还是交付。
        Returns:
            JSON: 开关/预算/视觉模型配置 + 最近一次 detect/solve 的结论(若有)。
        """
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
