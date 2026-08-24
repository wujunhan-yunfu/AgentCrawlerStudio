"""内容归档工具: 把 Agent 生成的文本/JSON/CSV/图片归档到虚拟文件系统。"""

from __future__ import annotations

import uuid

from langchain_core.tools import tool

from ...save import normalize_fmt, prepare_save
from ..core.fs import AGENT_SAVED_DIR, agent_real_path, agent_virtual_path
from ..session.model import AgentSession


def _save_file(data: str | bytes, ext: str = ".txt") -> str:
    saved = agent_real_path(AGENT_SAVED_DIR)
    saved.mkdir(parents=True, exist_ok=True)
    item_id = uuid.uuid4().hex[:8]
    path = saved / f"agent_{item_id}{ext}"
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return agent_virtual_path(path)


def build_save_tools(session: AgentSession) -> list:
    """内容归档工具。"""

    @tool
    async def archive_content(data: str, fmt: str = "txt") -> str:
        """把内容归档为文件, 返回文件路径。

        fmt 支持 txt(默认纯文本)/ json / jsonl / csv / img; img 时 data 为
        base64 图片字符串(data URI 或纯 base64), 文件后缀从图片 mime 自动读取。
        保存位置: 虚拟文件系统的 /agent_saved/。
        注意: 爬取任务的最终结果数据必须在 browser_run_code 脚本里用内置的
        save_page()/save_content() 保存(存到 /saved 并随运行结果返回),
        不要用本工具保存爬取数据; 本工具仅用于归档脚本等辅助文本。

        Args:
            data: 要保存的内容(纯文本 / JSON / CSV / base64 图片字符串)。
            fmt: 保存格式, txt(默认) / json / jsonl / csv / img。
        Returns:
            保存成功返回虚拟文件系统内的文件路径(如 /agent_saved/result.json),
            失败返回错误信息。
        """
        try:
            fmt = normalize_fmt(fmt)
            ext, raw, display = prepare_save(data, fmt)
            path = _save_file(raw, ext)
            session.emit(
                {
                    "type": "saved",
                    "saved": [
                        {
                            "id": uuid.uuid4().hex[:8],
                            "kind": "img" if fmt == "img" else "content",
                            "name": path.rsplit("/", 1)[-1],
                            "path": path,
                            "size": len(raw),
                            "content": display,
                        }
                    ],
                }
            )
            return f"已保存到 {path}"
        except Exception as exc:  # noqa: BLE001
            return f"保存失败: {exc}"

    return [archive_content]
