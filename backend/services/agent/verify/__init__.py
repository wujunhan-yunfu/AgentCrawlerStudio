"""人机验证处理包: 证据采集 → 类型判定(大模型为主/规则辅助) → 求解 → 出口复核。

产物运行期由 CrawlerEnv.verify_check 上下文驱动, 超限抛 VerificationFailed 结束本次运行;
Agent 开发期由 verify_gate 的会话内弹卡(仅 solve_verification 工具链)驱动。
详见 docs/human-verification-solution.md。
"""

from .classify import (
    TYPE_CHECKBOX,
    TYPE_NONE,
    TYPE_OCR,
    TYPE_POINTS,
    TYPE_SLIDER,
    Classifier,
    ClassifyResult,
    build_classifier,
    result_from_model,
    rule_classify,
)
from .coordinator import VerificationCoordinator, build_coordinator, should_engage
from .evidence import EVIDENCE_JS, best_container, has_clues, parse_evidence
from .exceptions import VerificationCancelled, VerificationFailed
from .ops import (
    BridgeOps,
    PageOps,
    PlaywrightOps,
    humanize_trajectory,
    to_data_uri,
)
from .scope import VerifyScope

__all__ = [
    "TYPE_CHECKBOX",
    "TYPE_NONE",
    "TYPE_OCR",
    "TYPE_POINTS",
    "TYPE_SLIDER",
    "Classifier",
    "ClassifyResult",
    "build_classifier",
    "result_from_model",
    "rule_classify",
    "VerificationCoordinator",
    "build_coordinator",
    "should_engage",
    "EVIDENCE_JS",
    "best_container",
    "has_clues",
    "parse_evidence",
    "VerificationCancelled",
    "VerificationFailed",
    "BridgeOps",
    "PageOps",
    "PlaywrightOps",
    "humanize_trajectory",
    "to_data_uri",
    "VerifyScope",
]
