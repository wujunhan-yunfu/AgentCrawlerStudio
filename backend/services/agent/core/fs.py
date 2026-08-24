"""Agent 虚拟文件系统: 磁盘 tmp 目录与 Agent 可见虚拟路径之间的映射。

agent 的虚拟文件系统根目录 `/` 即本项目 tmp 目录; /agent_saved、/agent_backend
为 agent 内可见的虚拟路径, 分别对应 tmp/agent_saved、tmp/agent_backend。
"""

from __future__ import annotations

from pathlib import Path

from deepagents.backends import FilesystemBackend

from ....config import PROJECT_ROOT

# agent 的虚拟文件系统根目录 `/` 即本项目 tmp 目录; /agent_saved、/agent_backend
# 为 agent 内可见的虚拟路径, 分别对应 tmp/agent_saved、tmp/agent_backend。
AGENT_TMP_ROOT = PROJECT_ROOT / "tmp"
AGENT_SAVED_DIR = "/agent_saved"
AGENT_BACKEND_DIR = "/agent_backend"
AGENT_BACKEND = FilesystemBackend(root_dir=str(AGENT_TMP_ROOT), virtual_mode=True)


def agent_real_path(virtual: str) -> Path:
    """把 agent 内的虚拟路径解析为磁盘上的真实路径(无需手动拼接)。"""
    return AGENT_BACKEND._resolve_path(virtual)


def agent_virtual_path(real: str | Path) -> str:
    """把磁盘上的真实路径转换为 agent 内可见的虚拟路径(反方向映射)。

    真实路径必须位于虚拟文件系统根目录(tmp/)之下, 否则回退为文件名。
    所有暴露给 Agent 的路径都必须是虚拟路径, 否则 Agent 的工具(ls/read/glob)
    会把真实绝对路径当作虚拟路径解析到 tmp/ 下, 导致 path_not_found。
    """
    path = Path(real)
    try:
        return AGENT_BACKEND._to_virtual_path(path)
    except (ValueError, OSError, RuntimeError):
        return path.name or "/"


def agent_sanitize(text: str) -> str:
    """把文本中出现的真实根目录前缀替换为虚拟根 `/`, 避免真实路径泄露给 Agent。

    例如 `/root/.../xvfb_test/tmp/saved/content_x.txt` -> `/saved/content_x.txt`。
    用于 browser_run_code 的输出/报错: 脚本里 print 出的 save_page()/save_content()
    真实路径若原样返回, Agent 会拿去 ls/read, 而虚拟文件系统无法解析真实绝对路径。
    """
    real = str(AGENT_TMP_ROOT).rstrip("/")
    return text.replace(real, "")
