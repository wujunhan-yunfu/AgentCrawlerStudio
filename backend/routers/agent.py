"""爬虫 Agent 路由: 会话管理 / 多轮消息 / 问卷 / 停止 / 状态。

所有会话接口按 crawler_id 隔离: 未显式传 crawler_id 时回退到后端配置的 crawler_id。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

router = APIRouter(tags=["agent"])


def _manager(request: Request):
    return request.app.state.agent


class SessionCreateRequest(BaseModel):
    title: str
    crawler_id: str | None = None


class SessionMessageRequest(BaseModel):
    content: str
    crawler_id: str | None = None


class SessionRenameRequest(BaseModel):
    title: str
    crawler_id: str | None = None


class StartRequest(BaseModel):
    task: str


class AnswerRequest(BaseModel):
    session_id: str
    qid: str
    answers: dict
    crawler_id: str | None = None


class StopRequest(BaseModel):
    session_id: str
    crawler_id: str | None = None


class FinalizeRequest(BaseModel):
    """前端第二层保障: 会话完成/停止后显式校正会话记录。"""

    status: str | None = None
    crawler_id: str | None = None


class LoginActionRequest(BaseModel):
    session_id: str
    action: str
    crawler_id: str | None = None


class LoginAnswerRequest(BaseModel):
    session_id: str
    qid: str
    answers: dict
    crawler_id: str | None = None


class EditorCodeRequest(BaseModel):
    code: str


@router.get("/agent/info")
async def agent_info(request: Request) -> dict:
    """返回当前后端配置的 crawler_id(会话隔离标识)。"""
    cfg = request.app.state.cfg
    return {"crawler_id": (cfg.crawler_id or "default") if cfg else "default"}


@router.post("/agent/session")
async def agent_session_create(request: Request, req: SessionCreateRequest) -> dict:
    """新建一个会话(可多轮对话, 意图由 Agent 自行判断), 返回 session_id。"""
    manager = _manager(request)
    try:
        session = await manager.create_session(req.crawler_id, req.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "session_id": session.id,
        "crawler_id": session.crawler_id,
        "title": session.title,
        "status": session.status,
    }


@router.get("/agent/sessions")
async def agent_sessions_list(request: Request) -> dict:
    """当前 crawler_id 的会话列表(按更新时间倒序)。"""
    crawler_id = request.query_params.get("crawler_id")
    sessions = await _manager(request).list_sessions(crawler_id)
    return {"sessions": sessions}


@router.get("/agent/session/{session_id}/messages")
async def agent_session_messages(request: Request, session_id: str) -> dict:
    """读取会话消息历史, 用于恢复对话。"""
    crawler_id = request.query_params.get("crawler_id")
    messages = await _manager(request).get_messages(crawler_id, session_id)
    return {"session_id": session_id, "messages": messages}


@router.post("/agent/session/{session_id}/message")
async def agent_session_message(request: Request, session_id: str,
                                req: SessionMessageRequest) -> dict:
    """向会话发送一条消息, 驱动 Agent 执行一轮多轮对话。"""
    manager = _manager(request)
    try:
        session = await manager.send_message(req.crawler_id, session_id, req.content)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "session_id": session.id,
        "status": session.status,
    }


@router.delete("/agent/session/{session_id}")
async def agent_session_delete(request: Request, session_id: str) -> dict:
    """删除会话及其全部消息。"""
    crawler_id = request.query_params.get("crawler_id")
    await _manager(request).delete_session(crawler_id, session_id)
    return {"ok": True}


@router.patch("/agent/session/{session_id}")
async def agent_session_rename(request: Request, session_id: str,
                               req: SessionRenameRequest) -> dict:
    """修改会话标题(用户手动改名)。"""
    manager = _manager(request)
    try:
        session = await manager.rename_session(req.crawler_id, session_id, req.title)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "session_id": session.id, "title": session.title}


@router.post("/agent/start")
async def agent_start(request: Request, req: StartRequest) -> dict:
    """兼容旧接口: 新建会话并立即以任务作为第一条消息启动。"""
    manager = _manager(request)
    try:
        session = await manager.start(req.task)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001  LLM 配置错误等
        raise HTTPException(status_code=400, detail=f"Agent 启动失败: {exc}") from exc
    return {
        "session_id": session.id,
        "status": session.status,
        "crawler_id": session.crawler_id,
    }


@router.get("/editor/code")
async def editor_code_get(request: Request) -> dict:
    """读取前端编辑器的当前代码(Agent 的后端镜像)。"""
    return {"ok": True, "code": _manager(request).editor_code()}


@router.post("/editor/code")
async def editor_code_set(request: Request, req: EditorCodeRequest) -> dict:
    """前端编辑器内容变化时同步到后端, 供 Agent 读取/回写。"""
    _manager(request).set_editor_code(req.code)
    return {"ok": True}


@router.websocket("/ws/agent")
async def ws_agent(ws: WebSocket):
    """订阅 Agent 事件流。query 参数 ?session=<id> 可只收某个会话的事件。"""
    await ws.accept()
    session_id = ws.query_params.get("session")
    manager = ws.app.state.agent
    sub = manager.hub.subscribe()
    try:
        hello_sessions = await manager.list_sessions(None)
        await ws.send_text(json.dumps({
            "type": "hello",
            "crawler_id": manager.default_crawler_id(),
            "sessions": hello_sessions,
        }, ensure_ascii=False))
        while True:
            try:
                event = await asyncio.wait_for(sub.get(), timeout=30)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
                continue
            if session_id and event.get("session_id") != session_id:
                continue
            try:
                await ws.send_text(json.dumps(event, ensure_ascii=False))
            except (WebSocketDisconnect, RuntimeError):
                break
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        manager.hub.unsubscribe(sub)


@router.post("/agent/answer")
async def agent_answer(request: Request, req: AnswerRequest) -> dict:
    """提交问卷答案, 恢复被 ask_user 打断的 Agent。"""
    manager = _manager(request)
    try:
        await manager.answer(req.crawler_id, req.session_id, req.qid, req.answers)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/agent/session/{session_id}/finalize")
async def agent_session_finalize(
    request: Request, session_id: str, req: FinalizeRequest
) -> dict:
    """前端会话完成/停止后调用: 以 MongoDB 实际消息为准校正会话记录。"""
    return await _manager(request).finalize_session(
        req.crawler_id, session_id, req.status
    )


@router.post("/agent/stop")
async def agent_stop(request: Request, req: StopRequest) -> dict:
    """停止指定会话。"""
    manager = _manager(request)
    try:
        await manager.stop(req.crawler_id, req.session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/agent/login-action")
async def agent_login_action(request: Request, req: LoginActionRequest) -> dict:
    """模拟登录框内的浏览器动作(如触发发送验证码 / 刷新图形验证码)。"""
    manager = _manager(request)
    try:
        return await manager.login_action(req.crawler_id, req.session_id, req.action)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/agent/login-answer")
async def agent_login_answer(request: Request, req: LoginAnswerRequest) -> dict:
    """提交模拟登录框的答案, 恢复被 page_login 挂起的脚本。"""
    manager = _manager(request)
    try:
        await manager.login_answer(
            req.crawler_id, req.session_id, req.qid, req.answers
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/agent/status")
async def agent_status(request: Request) -> dict:
    """兼容旧接口: 当前 crawler_id 的会话列表。"""
    crawler_id = request.query_params.get("crawler_id")
    return {"sessions": await _manager(request).list_sessions(crawler_id)}
