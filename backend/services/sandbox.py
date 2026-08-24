"""受限代码执行环境: 除 save_content / save_page 外禁止一切文件读写。

在 exec 用户爬虫脚本前, 用受限 builtins 替换内置环境:
- 移除 open 内建函数;
- 守卫 __import__, 禁止导入一切可用于文件读写/系统访问的模块
  (os / pathlib / shutil / subprocess / io / tempfile / glob / builtins / ...);
- 显式注入受限 builtins 字典, 避免用户代码经 __builtins__ 取回真实内建。

文件读写只允许通过注入的 save_content / save_page 完成。
"""

from __future__ import annotations

import builtins as _real_builtins
from typing import Any

# 禁止导入的模块: 文件读写 / 系统访问 / 逃逸通道
_BLOCKED_MODULES = frozenset({
    "os", "pathlib", "shutil", "subprocess", "io", "tempfile",
    "glob", "importlib", "pkgutil", "sys", "builtins", "codecs",
    "gzip", "bz2", "lzma", "zipfile", "tarfile", "pickle", "shelve",
    "marshal", "sqlite3", "posix", "nt", "resource", "readline",
    "ctypes", "winreg", "fcntl", "mmap", "copyreg", "dill",
    "cloudpickle", "joblib", "dis", "audit", "opcode", "types",
})

# 从 builtins 中移除的危险内建
_BLOCKED_BUILTINS = frozenset({"open"})

_real_import = _real_builtins.__import__


def _guarded_import(name: str, globals: Any = None, locals: Any = None,
                    fromlist: Any = (), level: int = 0) -> Any:
    root = (name or "").split(".")[0]
    if root in _BLOCKED_MODULES:
        raise ImportError(
            f"模块 {root} 已被禁用: 代码执行环境只允许通过 save_content / save_page 读写文件"
        )
    return _real_import(name, globals, locals, fromlist, level)


def safe_builtins() -> dict[str, Any]:
    """构建受限 builtins 字典: 无 open, __import__ 被守卫。"""
    base: dict[str, Any] = {
        k: v
        for k, v in vars(_real_builtins).items()
        if not k.startswith("__") or k in ("__import__",)
    }
    for name in _BLOCKED_BUILTINS:
        base.pop(name, None)
    base["__import__"] = _guarded_import
    return base
