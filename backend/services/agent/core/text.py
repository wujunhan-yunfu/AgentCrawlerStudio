"""文本处理小工具: 统一截断/格式化, 供各层复用。"""

from __future__ import annotations

from typing import Any


def cap_text(text: Any, limit: int) -> str:
    """把文本截断到指定长度, 超长时附截断提示。"""
    s = text if isinstance(text, str) else str(text)
    if len(s) > limit:
        return s[:limit] + f"\n...[已截断, 共 {len(s)} 字符]"
    return s
