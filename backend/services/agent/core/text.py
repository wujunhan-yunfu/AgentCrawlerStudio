"""文本处理小工具: 统一截断/格式化, 供各层复用。"""

from __future__ import annotations

from typing import Any


def cap_text(text: Any, limit: int) -> str:
    """把文本截断到指定长度, 超长时附截断提示。"""
    s = text if isinstance(text, str) else str(text)
    if len(s) > limit:
        return s[:limit] + f"\n...[已截断, 共 {len(s)} 字符]"
    return s


def collapse_cr(text: Any) -> str:
    """折叠 `\\r` 行内重绘帧: 每行只保留最后一帧。

    tqdm 等进度条用 `\\r` 原地刷新, 原始输出可能包含成百上千帧。直接交给模型
    既浪费上下文又会被 `cap_text` 截断到中途帧, 看不到最终结果; 折叠后每行只留
    最后(最新)一帧, 再交给 `cap_text` 截断才是有信息量的内容。
    前端展示走 ansiTerm 归一, 与本函数互不影响。
    """
    s = text if isinstance(text, str) else str(text)
    if "\r" not in s:
        return s
    lines: list[str] = []
    for line in s.split("\n"):
        if line.endswith("\r"):  # CRLF: 先去掉行尾 \r, 避免整行被清空
            line = line[:-1]
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        lines.append(line)
    return "\n".join(lines)


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
