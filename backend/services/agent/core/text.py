"""文本处理小工具: 统一截断/格式化, 供各层复用。"""

from __future__ import annotations

from typing import Any


def cap_text(text: Any, limit: int) -> str:
    """把文本截断到指定长度, 超长时附截断提示。"""
    s = text if isinstance(text, str) else str(text)
    if len(s) > limit:
        return s[:limit] + f"\n...[已截断, 共 {len(s)} 字符]"
    return s


def content_text(content: Any) -> str:
    """把 AIMessage.content(字符串或多模态 content block 列表)归一为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content) if content else ""
