"""规划与提问工具: 结构化规划 / 任务清单 / 向用户征询决策。"""

from __future__ import annotations

import json
import uuid

from langchain_core.tools import tool
from langgraph.types import interrupt

from ..session.model import AgentSession


def build_planning_tools(session: AgentSession) -> list:
    """规划与问卷工具。"""

    @tool
    async def record_plan(plan: str) -> str:
        """记录意图分析与爬取规划, 会展示在前端任务面板, 并约束你的执行流程。

        复杂任务动手前必须先调用一次。传入 JSON 字符串, 字段包括:
        goal(目标), candidate_sites(候选爬取网站及理由), scope(爬取范围/页数/条数),
        method(爬取方式: http 或 browser), login_required(是否需要登录),
        data_fields(要提取的字段), steps(执行步骤)。steps 的每一条用一句话描述一个
        可独立验证的动作, 必须完整覆盖整个任务; 之后严格按 steps 的顺序逐步执行,
        每完成一条就通过 write_todos 将其标记为 completed(内容需与 steps 一致, 才会同步
        更新前端 plan 状态), 可并行的步骤可以一次性完成; 任务结束前所有 steps 必须全部
        completed。规划若有变化可再次调用更新。
        Args:
            plan: JSON 字符串, 包括 goal / candidate_sites / scope / method /  login_required / data_fields / steps 等字段。
        Returns:
            规划已记录并在前端展示的确认信息。
        """
        try:
            parsed = json.loads(plan)
        except (ValueError, TypeError):
            parsed = {"raw": str(plan)}
        if not isinstance(parsed, dict):
            parsed = {"raw": str(parsed)}
        # steps 规范化为带状态的清单, 前端可实时显示每条步骤的进度
        steps = parsed.get("steps")
        if isinstance(steps, list):
            parsed["steps"] = [
                s if isinstance(s, dict) else {"content": str(s), "status": "pending"}
                for s in steps
            ]
        session.plan = parsed
        session.emit({"type": "plan", "plan": parsed})
        return "规划已记录并在前端展示"

    @tool
    async def ask_user(questions: str) -> str:
        """当无法在多个备选方案/网站中作出抉择, 或任务被风控阻断需要用户决定
        是否交付已生成结果时, 用本工具向用户提问。

        传入 JSON 数组, 每个元素: {"key": 唯一标识, "title": 问题描述,
        "type": "single"(单选) / "multi"(多选) / "text"(填空),
        "options": [候选选项...], "default": 默认值(可选)}。
        用户填写后返回其答案(JSON), 之后继续执行。
        仅在「多个方案无法抉择」「缺少完成任务必需的输入」或「被风控阻断需确认是否
        交付已生成结果」时才调用本工具。
        Args:
            questions: JSON 数组, 每个元素包括 key / title / type / options / default 等字段。
        Returns:
            用户填写后的答案(JSON 字符串), 以 key 为字段名。
        """
        try:
            questions_data = json.loads(questions)
        except (ValueError, TypeError):
            questions_data = [{"key": "q0", "title": str(questions), "type": "text"}]
        if not isinstance(questions_data, list):
            questions_data = [questions_data]
        payload = {
            "kind": "ask_user",
            "qid": uuid.uuid4().hex[:12],
            "questions": questions_data,
        }
        answers = interrupt(payload)
        return f"用户已确认: {json.dumps(answers, ensure_ascii=False)}"

    return [record_plan, ask_user]
