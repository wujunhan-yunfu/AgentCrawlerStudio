"""Agent 提示词包: 提示词与代码解耦, 独立存放于本目录, 便于单独维护与调优。

- system.md: 主系统提示词(爬虫 Agent 的身份 / 工作流 / 纪律)。
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """按文件名读取本目录下的提示词文件(name 不含扩展名)。"""
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


SYSTEM_PROMPT = load_prompt("system")

__all__ = ["SYSTEM_PROMPT", "load_prompt"]
