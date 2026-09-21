"""运行模式包: login / run 两种运行模式的执行器、webhook/心跳/数据推送与运行时。

仅由 CDC 编排场景使用: 通过 --mode login|run + --crawler-id 等参数驱动;
dev 模式不加载本包逻辑。
"""

from __future__ import annotations

from .run_context import LoginComplete, RunContext
from .run_mode import run_mode_main
from .webhook import WebhookSender, WebhookError

__all__ = [
    "LoginComplete",
    "RunContext",
    "run_mode_main",
    "WebhookSender",
    "WebhookError",
]