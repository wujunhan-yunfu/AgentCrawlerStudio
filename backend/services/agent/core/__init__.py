"""Agent 核心基础层: LLM 构建与虚拟文件系统工具。

本层不依赖会话/工具/运行时, 属于被其它模块复用的地基:
- llm.py:   OpenAI 兼容聊天模型构建
- fs.py:    Agent 虚拟文件系统(<-> 磁盘 tmp 目录)路径映射
- text.py:  文本截断等通用工具
"""

from .fs import (
    AGENT_BACKEND,
    AGENT_BACKEND_DIR,
    AGENT_SAVED_DIR,
    AGENT_TMP_ROOT,
    agent_real_path,
    agent_sanitize,
    agent_virtual_path,
)
from .llm import build_chat_model, resolve_base_url
from .text import cap_text

__all__ = [
    "AGENT_BACKEND",
    "AGENT_BACKEND_DIR",
    "AGENT_SAVED_DIR",
    "AGENT_TMP_ROOT",
    "agent_real_path",
    "agent_sanitize",
    "agent_virtual_path",
    "build_chat_model",
    "resolve_base_url",
    "cap_text",
]
