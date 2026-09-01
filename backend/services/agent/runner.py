"""Agent 管理器: 会话生命周期、多轮对话、流式事件转发、问卷打断与恢复。

会话(AgentSession)是可多轮问答的容器:
- 用户通过 send_message 往会话发送一条消息, 驱动 Agent 执行一轮。
- 每轮基于 langgraph astream(stream_mode=["updates","custom","messages"]) 消费:
  - updates:   todo 清单更新、__interrupt__ 问卷打断
  - custom:    中间件通过 runtime.stream_writer 发出的结构化事件
  - messages:  AI 文本增量(delta), 用于前端实时显示
- 会话/消息按 crawler_id 隔离并持久化到 MongoDB, 可在多轮间续聊。

对话状态通过 **MongoDB 异步 checkpointer**(motor 实现, thread_id=session_id)
持久化: 后端重启后 Agent 直接从 MongoDB 断点恢复完整上下文, 无需注入历史。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.types import Command

from .agent import build_agent
from .bridge import BrowserBridge
from .checkpointer import get_checkpointer
from .core.llm import build_chat_model
from .login import LoginGate
from .session import AgentSession, EditorState, EventHub
from .session.store import AgentStore

_STREAM_MODES = ["updates", "custom", "messages"]
_MAX_SESSIONS = 200

# 持久化这些类型的执行事件到消息历史, 供前端在会话恢复/刷新后重建执行记录
# (tool / tool_result / editor_code 用于还原工具执行框体与代码变更 diff 卡片)。
_PERSISTED_EVENT_TYPES = {
    "status",
    "error",
    "plan",
    "todos",
    "saved",
    "question",
    "tool",
    "tool_result",
    "editor_code",
    "login_request",
    "login_success",
    "login_action",
}
# 瞬时状态(思考中)不落库, 避免刷屏。
_TRANSIENT_STATUS = {"思考中..."}


class AgentManager:
    """管理所有爬虫 Agent 会话(多轮对话)。"""

    def __init__(self) -> None:
        self.hub = EventHub()
        self.sessions: dict[str, AgentSession] = {}
        self.cfg: Any = None
        self._stream: Any = None
        self.editor = EditorState()
        self.store: AgentStore | None = None

    # ---------------------------------------------------------- 初始化

    def setup(self, cfg: Any, stream: Any) -> None:
        self.cfg = cfg
        self._stream = stream
        self.store = AgentStore(cfg.mongo_uri, cfg.mongo_db)

    def _resolve_crawler_id(self, crawler_id: str | None) -> str:
        cid = crawler_id or (self.cfg.crawler_id if self.cfg else "") or ""
        return cid or "default"

    def default_crawler_id(self) -> str:
        return self._resolve_crawler_id(None)

    async def _store_call(
        self, method: str, *args: Any, default: Any = None, **kwargs: Any
    ) -> Any:
        """按方法名调用 store(全异步), MongoDB 不可用时优雅降级。"""
        if self.store is None:
            return default
        try:
            return await getattr(self.store, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001
            return default

    # ---------------------------------------------------------- 会话生命周期

    async def create_session(self, crawler_id: str | None, title: str) -> AgentSession:
        """新建一个会话(不立即启动 Agent, 等用户发第一条消息)。"""
        if not title or not title.strip():
            raise ValueError("会话标题不能为空")
        cid = self._resolve_crawler_id(crawler_id)
        session_id = uuid.uuid4().hex[:12]
        session = AgentSession(
            id=session_id,
            crawler_id=cid,
            title=title.strip(),
            hub=self.hub,
            status="idle",
        )
        # 先注册到内存并广播, 再异步落库, 让前端立即看到新会话
        self.sessions[session_id] = session
        self._evict_old()
        session.emit(
            {
                "type": "session_start",
                "title": session.title,
            }
        )
        loop = asyncio.get_running_loop()
        session.persist_task = loop.create_task(self._persist_session(session))
        if self.cfg is not None and session.agent is None:
            # 后台线程预构建 Agent(deepagents 构建约 1s, 冷启动), 消除首条消息的构建耗时
            try:
                session.build_task = loop.create_task(
                    asyncio.to_thread(self._ensure_agent, session)
                )
            except Exception:  # noqa: BLE001
                session.build_task = None
        return session

    async def start(self, task: str) -> AgentSession:
        """兼容旧接口: 新建会话并立即用任务作为第一条消息启动。"""
        if not task or not task.strip():
            raise ValueError("任务描述不能为空")
        if not self.cfg:
            raise RuntimeError("Agent 管理器未初始化")
        build_chat_model(self.cfg)  # 提前校验 LLM 配置, 便于返回明确的 HTTP 错误
        session = await self.create_session(None, task.strip())
        await self.send_message(session.crawler_id, session.id, task.strip())
        return session

    async def send_message(
        self, crawler_id: str | None, session_id: str, content: str
    ) -> AgentSession:
        """向会话发送一条用户消息, 驱动 Agent 执行一轮多轮对话。"""
        cid = self._resolve_crawler_id(crawler_id)
        session = self.sessions.get(session_id)
        if session is None:
            doc = await self._store_call("get_session", session_id, cid)
            if doc is None:
                raise KeyError("会话不存在")
            session = AgentSession(
                id=doc["session_id"],
                crawler_id=doc["crawler_id"],
                title=doc["title"],
                hub=self.hub,
                status=doc.get("status", "idle"),
            )
            self.sessions[session_id] = session
        if session.status in ("running", "waiting"):
            raise ValueError("会话正在处理中, 请等待当前任务完成后再发送")
        if not content or not content.strip():
            raise ValueError("消息内容不能为空")
        content = content.strip()
        # 新一轮开始: 把当前编辑器内容记为「源文件基线」, 供前端展示总变更
        self.editor.mark_turn()
        # 首条消息: 后台自动生成会话标题(不阻塞消息发送), 手动改名后不再覆盖
        if not session.started and not session.title_manual:
            is_first = (
                await self._store_call(
                    "count_messages", session.id, session.crawler_id, default=0
                )
                or 0
            ) == 0
            if is_first:
                loop = asyncio.get_running_loop()
                loop.create_task(self._auto_title(session, content))
        if session.agent is None:
            # 优先等待后台预构建的 Agent; 未完成/构建失败则现场构建
            build_task = getattr(session, "build_task", None)
            if build_task is not None and not build_task.done():
                try:
                    await build_task
                except Exception:  # noqa: BLE001
                    pass
            if session.agent is None:
                try:
                    self._ensure_agent(session)
                except RuntimeError as exc:
                    raise ValueError(str(exc)) from exc
        # 先广播用户消息回显, 让前端立即看到, 再落库
        session.status = "running"
        session.emit(
            {"type": "user_message", "content": content, "title": session.title}
        )
        # 落库本条用户消息(需在 _run_turn 读取历史之前完成, 保证首轮上下文完整)
        await self._store_call(
            "add_message", session.id, session.crawler_id, "user", "text", content
        )
        loop = asyncio.get_running_loop()
        # 消息计数与会话状态更新放到后台, 不阻塞响应
        loop.create_task(self._persist_turn_start(session, content))
        session.task_handle = loop.create_task(self._run_turn(session, content))
        return session

    async def delete_session(self, crawler_id: str | None, session_id: str) -> None:
        """删除会话、其全部消息与对应的检查点状态。"""
        cid = self._resolve_crawler_id(crawler_id)
        session = self.sessions.pop(session_id, None)
        if session is not None and session.status in ("running", "waiting"):
            if session.answer_future is not None and not session.answer_future.done():
                session.answer_future.cancel()
            if session.login_future is not None and not session.login_future.done():
                session.login_future.cancel()
            if session.task_handle is not None:
                session.task_handle.cancel()
        await self._store_call("delete_session", session_id, cid)
        try:
            await get_checkpointer(self.cfg).adelete_thread(session_id)
        except Exception:  # noqa: BLE001
            pass
        self.hub.emit(
            {"type": "session_deleted", "session_id": session_id, "crawler_id": cid}
        )

    async def rename_session(
        self, crawler_id: str | None, session_id: str, title: str
    ) -> AgentSession:
        """修改会话标题(用户手动改名), 广播 session_rename 事件。"""
        if not title or not title.strip():
            raise ValueError("会话标题不能为空")
        cid = self._resolve_crawler_id(crawler_id)
        session = self.sessions.get(session_id)
        if session is None:
            doc = await self._store_call("get_session", session_id, cid)
            if doc is None:
                raise KeyError("会话不存在")
            session = AgentSession(
                id=doc["session_id"],
                crawler_id=doc["crawler_id"],
                title=doc["title"],
                hub=self.hub,
                status=doc.get("status", "idle"),
            )
            self.sessions[session_id] = session
        session.title = title.strip()
        session.title_manual = True
        await self._store_call(
            "update_session", session.id, session.crawler_id, title=session.title
        )
        session.emit({"type": "session_rename", "title": session.title})
        return session

    async def list_sessions(self, crawler_id: str | None) -> list[dict[str, Any]]:
        """当前 crawler_id 的会话列表(含实时状态覆盖)。"""
        cid = self._resolve_crawler_id(crawler_id)
        docs = await self._store_call("list_sessions", cid, default=[]) or []
        result: list[dict[str, Any]] = []
        for doc in docs:
            live = self.sessions.get(doc["session_id"])
            if live is not None:
                doc["status"] = live.status
                doc["plan"] = live.plan
                doc["question"] = live.question
            result.append(doc)
        return result

    async def get_messages(
        self, crawler_id: str | None, session_id: str
    ) -> list[dict[str, Any]]:
        """读取会话消息历史(用于前端恢复对话)。"""
        return (
            await self._store_call(
                "list_messages",
                session_id,
                self._resolve_crawler_id(crawler_id),
                default=[],
            )
            or []
        )

    async def _persist_session(self, session: AgentSession) -> None:
        """后台落库新创建的会话, 不阻塞会话创建的响应。"""
        await self._store_call(
            "create_session", session.id, session.crawler_id, session.title
        )

    async def _persist_turn_start(self, session: AgentSession, content: str) -> None:
        """后台更新消息计数与会话状态, 不阻塞消息发送的响应。

        若会话刚创建且落库任务未完成, 先等待其完成, 保证会话文档先于状态更新存在。
        """
        persist_task = getattr(session, "persist_task", None)
        if persist_task is not None and not persist_task.done():
            try:
                await persist_task
            except Exception:  # noqa: BLE001
                pass
        count = (
            await self._store_call(
                "count_messages", session.id, session.crawler_id, default=0
            )
            or 0
        )
        await self._store_call(
            "update_session",
            session.id,
            session.crawler_id,
            status="running",
            last_message=content,
            message_count=count,
        )

    # ---------------------------------------------------------- 自动标题

    async def _auto_title(self, session: AgentSession, content: str) -> None:
        """后台生成会话标题并落库/广播(用户手动改名后跳过)。"""
        try:
            title = await self._generate_title(content)
        except Exception:  # noqa: BLE001
            title = self._fallback_title(content)
        title = (title or "").strip()
        if not title or session.title_manual:
            return
        session.title = title[:30]
        await self._store_call(
            "update_session", session.id, session.crawler_id, title=session.title
        )
        session.emit({"type": "session_rename", "title": session.title})

    async def _generate_title(self, content: str) -> str:
        """用 LLM 把用户首条消息压缩成一个简短标题, 失败时回退到截断文本。"""
        try:
            model = build_chat_model(self.cfg)
        except Exception:  # noqa: BLE001
            return self._fallback_title(content)
        try:
            resp = await model.ainvoke(
                [
                    (
                        "system",
                        "你负责给用户的爬虫任务对话生成简洁标题, 只返回标题本身。",
                    ),
                    (
                        "human",
                        "请根据用户的第一条消息, 生成一个不超过 20 个字的简短中文标题, "
                        "直接返回标题, 不要引号、不要解释。\n用户消息: "
                        + content[:200],
                    ),
                ]
            )
            raw = getattr(resp, "content", "") or ""
            if isinstance(raw, list):
                raw = "".join(
                    str(p.get("text", ""))
                    for p in raw
                    if isinstance(p, dict) and p.get("text")
                )
            text = str(raw).strip().strip('"').strip("'").strip()
            return (text.splitlines()[0] if text else "").strip()
        except Exception:  # noqa: BLE001
            return self._fallback_title(content)

    @staticmethod
    def _fallback_title(content: str) -> str:
        first = next(
            (line.strip() for line in content.splitlines() if line.strip()), ""
        )
        return (first or content.strip())[:30]

    def _evict_old(self) -> None:
        if len(self.sessions) <= _MAX_SESSIONS:
            return
        for sid in sorted(self.sessions, key=lambda s: self.sessions[s].created_at)[
            :-_MAX_SESSIONS
        ]:
            old = self.sessions[sid]
            if old.status in ("done", "error", "cancelled", "idle"):
                self.sessions.pop(sid, None)

    # ---------------------------------------------------------- 控制接口

    async def answer(
        self, crawler_id: str | None, session_id: str, qid: str, answers: Any
    ) -> None:
        """提交问卷答案, 恢复被 ask_user 打断的 Agent。"""
        session = self.sessions.get(session_id)
        if session is None or session.crawler_id != self._resolve_crawler_id(
            crawler_id
        ):
            raise KeyError("会话不存在")
        if session.answer_future is None or session.status != "waiting":
            raise ValueError("当前没有等待中的问题")
        if session.question is None or session.question.get("qid") != qid:
            raise ValueError("问题编号不匹配")
        session.emit({"type": "answer_received", "qid": qid})
        session.answer_future.set_result(answers)

    async def login_action(
        self, crawler_id: str | None, session_id: str, action: str
    ) -> dict:
        """执行模拟登录框内的浏览器动作(发送验证码 / 刷新图形验证码)。"""
        session = self.sessions.get(session_id)
        if session is None or session.crawler_id != self._resolve_crawler_id(
            crawler_id
        ):
            raise KeyError("会话不存在")
        if session.login is None or session.status != "waiting":
            raise ValueError("当前没有等待中的登录")
        gate = LoginGate(session, BrowserBridge(self._stream))
        if action == "send_code":
            return await gate.send_code()
        if action == "refresh_captcha":
            return await gate.refresh_captcha()
        if action == "refresh_qr":
            return await gate.refresh_qr()
        raise ValueError(f"未知登录动作: {action}")

    async def login_answer(
        self, crawler_id: str | None, session_id: str, qid: str, answers: Any
    ) -> None:
        """提交模拟登录框的答案, 恢复被 page_login 挂起的脚本。"""
        session = self.sessions.get(session_id)
        if session is None or session.crawler_id != self._resolve_crawler_id(
            crawler_id
        ):
            raise KeyError("会话不存在")
        if session.login_future is None or session.status != "waiting":
            raise ValueError("当前没有等待中的登录")
        if session.login is None or session.login.get("qid") != qid:
            raise ValueError("登录请求编号不匹配")
        session.emit({"type": "answer_received", "qid": qid})
        session.login_future.set_result(answers)

    async def stop(self, crawler_id: str | None, session_id: str) -> None:
        """停止指定会话的当前轮次。"""
        session = self.sessions.get(session_id)
        if session is None or session.crawler_id != self._resolve_crawler_id(
            crawler_id
        ):
            raise KeyError("会话不存在")
        if session.status not in ("running", "waiting"):
            return
        if session.answer_future is not None and not session.answer_future.done():
            session.answer_future.cancel()
        if session.login_future is not None and not session.login_future.done():
            session.login_future.cancel()
        if session.task_handle is not None:
            session.task_handle.cancel()
        await self._store_call(
            "update_session", session.id, session.crawler_id, status="cancelled"
        )

    async def finalize_session(
        self,
        crawler_id: str | None,
        session_id: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        """前端在会话完成/停止后调用的第二层保障: 校正会话记录。

        后端在每轮结束时已写库(_finalize_done / stop), 此处由前端显式再触发一次,
        以 MongoDB 中的实际消息为准重算 message_count / last_message / status,
        保证即使异步落库时序异常, 会话文档也始终与消息历史一致。
        """
        cid = self._resolve_crawler_id(crawler_id)
        msgs = (
            await self._store_call("list_messages", session_id, cid, default=[])
            or []
        )
        count = len(msgs)
        last_message = ""
        for m in reversed(msgs):
            if m.get("role") in ("user", "assistant"):
                last_message = m.get("content", "") or ""
                break
        doc = await self._store_call("get_session", session_id, cid)
        current_status = (doc or {}).get("status", "done")
        new_status = status or current_status
        await self._store_call(
            "update_session",
            session_id,
            cid,
            status=new_status,
            message_count=count,
            last_message=last_message or (doc or {}).get("last_message", ""),
        )
        session = self.sessions.get(session_id)
        if session is not None:
            session.status = new_status
        return {
            "ok": True,
            "session_id": session_id,
            "status": new_status,
            "message_count": count,
        }

    def editor_code(self) -> str:
        return self.editor.get()

    def set_editor_code(self, code: str) -> None:
        self.editor.set(code)

    # ---------------------------------------------------------- 主运行循环

    def _ensure_agent(self, session: AgentSession) -> None:
        """为会话惰性构建统一 Agent(首次发消息时), 并绑定独立 checkpointer。"""
        build_chat_model(self.cfg)  # 提前校验 LLM 配置
        bridge = BrowserBridge(self._stream)
        session.persist = self._persist_event
        session.agent = build_agent(self.cfg, session, bridge, editor=self.editor)
        session.config = {"configurable": {"thread_id": session.id}}

    async def _load_history(self, session: AgentSession) -> list:
        """从 MongoDB 读取会话历史, 用于后端重启后重建上下文。"""
        docs = (
            await self._store_call(
                "list_messages", session.id, session.crawler_id, default=[]
            )
            or []
        )
        history: list = []
        for m in docs:
            if m.get("role") == "user":
                history.append(HumanMessage(content=m.get("content", "")))
            elif m.get("role") == "assistant":
                history.append(AIMessage(content=m.get("content", "")))
        return history

    async def _thread_has_history(self, session: AgentSession) -> bool:
        """判断该会话在 MongoDB checkpointer 中是否已存在对话状态。"""
        try:
            state = await session.agent.aget_state(session.config)
            messages = (state.values or {}).get("messages") or []
            return len(messages) > 0
        except Exception:  # noqa: BLE001
            return False

    async def _run_turn(self, session: AgentSession, content: str) -> None:
        """执行一轮对话: 续接 MongoDB checkpointer 的完整历史。

        检查点按 thread_id(= 会话 id) 持久化在 MongoDB:
        - 进程内续聊: 直接追加本轮新消息, checkpointer 自动续接已保存的历史;
        - 后端重启后: checkpointer 仍持有完整状态, 同样只需传新消息;
        - 仅有消息记录而检查点为空(如旧数据): 回退到从 MongoDB 注入历史。
        """
        session.error = None
        session.question = None
        if not session.started:
            session.started = True
            if await self._thread_has_history(session):
                # checkpointer 已保存历史, 直接续接, 避免重复注入
                input_messages = [HumanMessage(content=content)]
            else:
                # 无检查点: 从 MongoDB 注入历史作为初始上下文(含本条消息)
                history = await self._load_history(session)
                input_messages = history or [HumanMessage(content=content)]
        else:
            input_messages = [HumanMessage(content=content)]
        try:
            await self._consume(
                session,
                session.agent.astream(
                    {"messages": input_messages},
                    config=session.config,
                    stream_mode=_STREAM_MODES,
                ),
            )
            await self._finalize_done(session)
        except asyncio.CancelledError:
            session.status = "cancelled"
            session.emit({"type": "status", "content": "任务已取消"})
            await self._store_call(
                "update_session", session.id, session.crawler_id, status="cancelled"
            )
            session.emit({"type": "session_end", "reason": "cancelled"})
        except Exception as exc:  # noqa: BLE001
            session.status = "error"
            session.error = f"{type(exc).__name__}: {exc}"
            session.emit({"type": "status", "content": f"任务出错: {session.error}"})
            await self._persist_event(
                session, {"type": "error", "content": session.error}
            )
            session.emit({"type": "error", "error": session.error})
            await self._store_call(
                "update_session", session.id, session.crawler_id, status="error"
            )
            session.emit({"type": "session_end", "reason": "error"})
        finally:
            session.task_handle = None

    async def _consume(self, session: AgentSession, stream) -> None:
        """消费一条(可能被多次 resume 续接的)事件流。"""
        async for mode, data in stream:
            if mode == "updates" and "__interrupt__" in data:
                interrupt_payload = data["__interrupt__"][0]
                answers = await self._handle_question(session, interrupt_payload)
                if not answers:
                    answers = {"_empty": True}
                resumed = session.agent.astream(
                    Command(resume=answers),
                    config=session.config,
                    stream_mode=_STREAM_MODES,
                )
                await self._consume(session, resumed)
                return
            await self._handle_event(session, mode, data)

    async def _handle_event(self, session: AgentSession, mode: str, data: Any) -> None:
        if mode == "custom":
            if isinstance(data, dict):
                await self._persist_event(session, data)
                session.emit(data)
        elif mode == "messages":
            try:
                chunk, _meta = data
            except (ValueError, TypeError):
                return
            if (
                isinstance(chunk, AIMessageChunk)
                and chunk.content
                and not chunk.tool_call_chunks
            ):
                session.emit({"type": "delta", "content": chunk.content})
        elif mode == "updates":
            if isinstance(data, dict):
                for _node, update in data.items():
                    if isinstance(update, dict) and update.get("todos") is not None:
                        todos = update["todos"]
                        await self._persist_event(
                            session, {"type": "todos", "todos": todos}
                        )
                        session.emit({"type": "todos", "todos": todos})
                        # 把 todo 的进度同步到 plan 的 steps, 前端立刻更新 plan 状态
                        updated_plan = self._sync_plan_status(session, todos)
                        if updated_plan is not None:
                            await self._persist_event(
                                session, {"type": "plan", "plan": updated_plan}
                            )
                            session.emit({"type": "plan", "plan": updated_plan})

    def _sync_plan_status(
        self, session: AgentSession, todos: Any
    ) -> dict[str, Any] | None:
        """把 write_todos 的进度按内容匹配到 plan.steps, 返回更新后的 plan(无变化返回 None)。"""
        plan = session.plan
        if not isinstance(plan, dict) or "steps" not in plan:
            return None
        steps = plan.get("steps")
        if not isinstance(steps, list):
            return None
        status_by_content: dict[str, str] = {}
        for t in todos or []:
            if isinstance(t, dict) and t.get("content") is not None:
                status_by_content[str(t["content"])] = str(t.get("status", "pending"))
        if not status_by_content:
            return None
        new_steps: list[dict[str, Any]] = []
        for s in steps:
            if isinstance(s, dict):
                content = str(s.get("content", ""))
                status = status_by_content.get(content, str(s.get("status", "pending")))
                new_steps.append({"content": content, "status": status})
            else:
                content = str(s)
                new_steps.append(
                    {
                        "content": content,
                        "status": status_by_content.get(content, "pending"),
                    }
                )
        seen = {s["content"] for s in new_steps}
        for t in todos or []:
            if isinstance(t, dict) and t.get("content") is not None:
                content = str(t["content"])
                if content not in seen:
                    new_steps.append(
                        {"content": content, "status": str(t.get("status", "pending"))}
                    )
                    seen.add(content)
        updated = dict(plan)
        updated["steps"] = new_steps
        session.plan = updated
        return updated

    async def _persist_event(
        self, session: AgentSession, event: dict[str, Any]
    ) -> None:
        """把重要的执行事件写入消息历史, 便于会话恢复后复盘。"""
        etype = event.get("type")
        if etype not in _PERSISTED_EVENT_TYPES:
            return
        content = ""
        meta: dict[str, Any] = {}
        if etype in ("status", "error"):
            content = event.get("content", "") or ""
            if etype == "status" and content in _TRANSIENT_STATUS:
                return
        elif etype == "plan":
            meta = {"plan": event.get("plan")}
        elif etype == "todos":
            meta = {"todos": event.get("todos")}
        elif etype == "saved":
            saved = event.get("saved") or []
            content = f"已保存 {len(saved)} 项内容"
            meta = {"saved": saved}
        elif etype == "question":
            meta = {"qid": event.get("qid"), "questions": event.get("questions")}
        elif etype == "tool":
            args = event.get("args", "") or ""
            content = args if isinstance(args, str) else str(args)
            meta = {"id": event.get("id"), "name": event.get("name")}
        elif etype == "tool_result":
            content = event.get("content", "") or ""
            meta = {
                "id": event.get("id"),
                "name": event.get("name"),
                "error": event.get("error", "") or "",
            }
        elif etype == "editor_code":
            content = event.get("code", "") or ""
            meta = {"base": event.get("base", "") or ""}
        elif etype == "login_request":
            content = "需要用户完成登录"
            meta = {"login": event.get("login") or event.get("meta", {}).get("login")}
        elif etype == "login_success":
            content = f"登录成功, 跳转到 {event.get('url') or ''}".strip()
            meta = {"method": event.get("method"), "url": event.get("url")}
        elif etype == "login_action":
            content = event.get("message", "") or ""
            meta = {"action": event.get("action"), "ok": event.get("ok")}
        await self._store_call(
            "add_message", session.id, session.crawler_id, "event", etype, content, meta
        )

    async def _handle_question(
        self, session: AgentSession, interrupt_payload: Any
    ) -> Any:
        payload = getattr(interrupt_payload, "value", interrupt_payload)
        if not isinstance(payload, dict) or payload.get("kind") != "ask_user":
            session.emit(
                {"type": "status", "content": f"遇到未预期的打断: {payload!r}"}
            )
            return None
        session.status = "waiting"
        session.question = {
            "qid": payload.get("qid"),
            "questions": payload.get("questions"),
        }
        session.answer_future = asyncio.get_running_loop().create_future()
        await self._persist_event(
            session,
            {
                "type": "question",
                "qid": payload.get("qid"),
                "questions": payload.get("questions"),
            },
        )
        session.emit(
            {
                "type": "question",
                "qid": payload.get("qid"),
                "questions": payload.get("questions"),
            }
        )
        try:
            answers = await session.answer_future
        finally:
            session.answer_future = None
        session.question = None
        session.status = "running"
        return answers

    async def _finalize_done(self, session: AgentSession) -> None:
        try:
            state = await session.agent.aget_state(session.config)
        except Exception:  # noqa: BLE001
            state = None
        result = ""
        if state is not None:
            messages = (state.values or {}).get("messages") or []
            for m in reversed(messages):
                if isinstance(m, AIMessage) and m.content and not m.tool_calls:
                    content = m.content
                    result = content if isinstance(content, str) else str(content)
                    break
        if result:
            await self._store_call(
                "add_message",
                session.id,
                session.crawler_id,
                "assistant",
                "text",
                result,
            )
        count = (
            await self._store_call(
                "count_messages", session.id, session.crawler_id, default=0
            )
            or 0
        )
        await self._store_call(
            "update_session",
            session.id,
            session.crawler_id,
            status="done",
            last_message=result or session.title,
            message_count=count,
        )
        session.status = "done"
        session.emit({"type": "done", "result": result})
        session.emit({"type": "session_end", "reason": "done"})
