"""代码来源: 从 MongoDB 读取 crawler_id 的 HEAD 代码(供 login/run 模式)。"""

from __future__ import annotations

from typing import Any

from ..code_version import CodeStore


class NoCodeError(Exception):
    """Mongo 中不存在可用代码(无 HEAD 提交)。"""


async def load_head_code(cfg: Any) -> str:
    """读取当前 crawler_id 的 HEAD 提交内容; 无提交/不可达时抛 NoCodeError。"""
    crawler_id = (getattr(cfg, "crawler_id", "") or "default").strip() or "default"
    store = CodeStore(getattr(cfg, "mongo_uri", ""), getattr(cfg, "mongo_db", "crawler"))
    try:
        head = await store.get_head(crawler_id)
        if not head:
            raise NoCodeError(f"crawler_id={crawler_id} 没有已提交代码(HEAD 为空)")
        commit = await store.get_commit(crawler_id, head)
    except NoCodeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise NoCodeError(f"读取代码失败(Mongo 不可用或提交缺失): {exc}") from exc
    if not commit or not commit.get("content"):
        raise NoCodeError(f"crawler_id={crawler_id} 的 HEAD 提交内容为空")
    return commit["content"]