"""临时目录清理: 启动时回收崩溃残留的 Chrome profile / 运行产物。

只删除超过保留期的目录, 避免误删正在使用的文件; 所有异常静默(清理失败不影响启动)。
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_MAX_AGE = 24 * 3600


def _age_seconds(path: Path, now: float) -> float:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return 0.0


def _is_old(path: Path, now: float, max_age: float) -> bool:
    return _age_seconds(path, now) > max_age


def sweep_temp_dirs(
    project_root: Path,
    max_age: float = _DEFAULT_MAX_AGE,
    *,
    now: float | None = None,
) -> int:
    """删除项目临时目录中超过 max_age 秒的 profile/运行产物, 返回删除数量。

    覆盖:
    - `.chrome-profile/*`(当前 profile 根)
    - `tmp/chrome-profile-*`(旧版本遗留)
    - `tmp/runs/*`
    - `tmp/saved/*`、`tmp/agent_saved/*`、`tmp/conversation_history/*`
    """
    root = Path(project_root)
    current = time.time() if now is None else now
    candidates: list[Path] = []

    profile_root = root / ".chrome-profile"
    if profile_root.is_dir():
        candidates.extend(p for p in profile_root.iterdir() if p.is_dir())

    tmp_root = root / "tmp"
    if tmp_root.is_dir():
        candidates.extend(tmp_root.glob("chrome-profile-*"))
        runs = tmp_root / "runs"
        if runs.is_dir():
            candidates.extend(p for p in runs.iterdir() if p.is_dir())
        for name in ("saved", "agent_saved", "conversation_history"):
            folder = tmp_root / name
            if folder.is_dir():
                candidates.extend(folder.iterdir())

    removed = 0
    for path in candidates:
        if not _is_old(path, current, max_age):
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:  # pragma: no cover - 权限等环境问题
            log.warning("清理临时目录失败 %s: %s", path, exc)
    return removed
