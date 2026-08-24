"""编辑器读写工具: 读取/回写前端编辑器代码 + 临时脚本调试。"""

from __future__ import annotations

import uuid

from langchain_core.tools import tool

from ..core.fs import (
    AGENT_BACKEND_DIR,
    agent_real_path,
    agent_sanitize,
    agent_virtual_path,
)
from ..core.text import cap_text
from ..session.model import AgentSession, EditorState
from ..bridge import BrowserBridge
from ..login import LoginGate


def build_editor_tools(
    session: AgentSession, bridge: BrowserBridge, editor: EditorState | None = None
) -> list:
    """编辑器相关工具: 读代码 / 临时脚本调试 / 回写代码。"""

    @tool
    async def get_editor_code() -> str:
        """读取用户当前在前端代码编辑器中的完整代码。

        统一 Agent 的第一步: 先读编辑器里的代码, 再判断用户是要采集数据还是要
        修改/优化这段代码。返回的是 Python 源码字符串。
        Args:
            无参数, 直接读取前端编辑器中的代码。
        Returns:
            Python 源码字符串, 或提示编辑器为空。
        """
        code = editor.get() if editor is not None else ""
        if not code:
            return "(编辑器当前为空, 无代码)"
        return code

    @tool
    async def debug_code(code: str, filename: str = "") -> str:
        """把一段待验证的 Python 代码写成临时调试脚本并立即在项目浏览器中运行。

        这是 Agent 修改编辑器前的「临时脚本调试」工具: 在改动编辑器之前, 先把
        待验证/待调试的代码写到虚拟文件系统 /agent_backend/ 下的临时脚本里运行,
        通过输出/报错反复迭代; 确认跑通后再用 set_editor_code 一次性同步到编辑器。
        临时脚本不会影响前端编辑器内容。脚本为 async 风格, 使用 page/context/
        browser 及内置函数时需加 await(如 `await page.goto(url)`)。
        Args:
            code: Python 源码字符串, 可直接使用内置浏览器对象与函数。
            filename: 可选临时脚本文件名(默认自动生成 debug_*.py)。
        Returns:
            临时脚本路径与运行结果(ok/输出/错误)。
        """
        name = (filename or "").strip() or f"debug_{uuid.uuid4().hex[:6]}.py"
        if not name.endswith(".py"):
            name += ".py"
        try:
            real = agent_real_path(AGENT_BACKEND_DIR) / name
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_text(code, encoding="utf-8")
            vpath = agent_virtual_path(real)
        except Exception as exc:  # noqa: BLE001
            return f"临时脚本生成失败: {exc}"
        session.emit({"type": "debug_script", "path": vpath, "name": name})
        result = await bridge.run_code(
            code, login_gate=LoginGate(session, bridge)
        )
        saved = result.get("saved") or []
        if saved:
            session.emit({"type": "saved", "saved": saved})
        out = agent_sanitize(result.get("output") or "")
        err = agent_sanitize(result.get("error") or "")
        parts = [f"临时脚本: {vpath}", f"ok={result.get('ok')}"]
        if out:
            parts.append(f"输出:\n{cap_text(out, 3000)}")
        if err:
            parts.append(f"错误:\n{cap_text(err, 3000)}")
        if saved:
            parts.append(f"已保存 {len(saved)} 项内容")
        return "\n".join(parts)

    @tool
    async def set_editor_code(code: str) -> str:
        """把最终/优化后的代码写回用户的前端代码编辑器。

        无论采集还是改代码, 任务交付都必须调用本工具把完整可复用的脚本写回编辑器,
        用户可立即查看/再运行。code 为完整的 Python 源码。
        注意: 只有在临时脚本(debug_code)验证跑通后才调用本工具一次性同步到编辑器,
        不要在调试过程中反复回写; 前端会展示每次写回相对上一次修改/源文件的差异。
        Args:
            code: Python 源码字符串, 由 Agent 修改/调试后生成。
        Returns:
            提示已写回编辑器, 及代码长度。
        """
        if editor is None:
            return "编辑器不可用"
        editor.set(code)
        session.emit(
            {
                "type": "editor_code",
                "code": code,
                "base": getattr(editor, "base_code", ""),
            }
        )
        return f"已写回编辑器, 共 {len(code)} 字符"

    return [get_editor_code, debug_code, set_editor_code]
