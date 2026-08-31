"""控制接口路由: 状态/页面/导航/截图/运行代码/重启"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ..schemas import (
    CookieDeleteRequest,
    CookieSetRequest,
    DomBoxRequest,
    EvalRequest,
    EvalResult,
    FormatRequest,
    FormatResult,
    IdbDataRequest,
    IdbStoresRequest,
    RunLoginActionRequest,
    RunLoginAnswerRequest,
    NavigateRequest,
    NavigateResult,
    NetworkBodyRequest,
    PageInfo,
    PropertiesRequest,
    PropertiesResult,
    RunLoginAnswerResult,
    RunLoginResult,
    RunRequest,
    StatusResult,
    StorageItemsRequest,
    StorageSetRequest,
)
from ..services.agent.bridge import BrowserBridge
from ..services.agent.run_login import RunLoginManager
from ..services.browser import BrowserError

router = APIRouter(tags=["control"])

# 流式输出空闲保活间隔: 脚本挂起(page_login 等)期间持续发送心跳, 防止前端超时
_HEARTBEAT_INTERVAL = 5.0


def _run_code_worker(stream: Any, code: str, login_gate: Any,
                     on_output: Callable[[str], None]) -> dict:
    """在独立线程+独立事件循环中执行 run_code。

    用户脚本(含 time.sleep 等阻塞调用)在单独事件循环里运行, 不阻塞主事件循环,
    从而保证 stdout 实时流式输出、空闲心跳与最终 done 标记能及时送达前端。
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(
            stream.run_code(code, login_gate=login_gate, on_output=on_output)
        )
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        loop.close()
        asyncio.set_event_loop(None)


def _stream(request: Request):
    return request.app.state.stream


def _run_login(request: Request) -> RunLoginManager:
    return request.app.state.run_login


def _sse_event(obj: dict) -> str:
    """SSE 事件帧: `data: <json>\\n\\n`, 浏览器/代理对 text/event-stream 不做缓冲。"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@router.get("/status", response_model=StatusResult)
async def status(request: Request) -> dict:
    return await _stream(request).status()


@router.get("/pages", response_model=list[PageInfo])
async def pages(request: Request) -> list[dict]:
    return await _stream(request)._cdp_pages()


@router.post("/navigate", response_model=NavigateResult)
async def navigate(request: Request, req: NavigateRequest) -> dict:
    stream = _stream(request)
    try:
        return await stream.navigate(req.url, req.new_page)
    except BrowserError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/screenshot")
async def screenshot(request: Request) -> Response:
    stream = _stream(request)
    try:
        data = await stream.screenshot()
    except BrowserError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=data, media_type="image/png")


@router.post("/run")
async def run(request: Request, req: RunRequest) -> StreamingResponse:
    """执行 Playwright 代码(SSE 流式): 每次自动重启全新无痕浏览器。

    返回 Server-Sent Events (text/event-stream), 每个事件 `data: <json>\\n\\n`,
    每个事件都带 ts(毫秒时间戳):
      - data: {"type": "start", "run_id": ..., "ts": ...}       开始
      - data: {"type": "stdout", "data": "...", "ts": ...}      实时 stdout/stderr 输出
      - data: {"type": "heartbeat", "ts": ...}                  空闲保活(脚本挂起时保持连接)
      - data: {"type": "done", "result": {...}, "ts": ...}      最终结果
    text/event-stream 不会被浏览器/反向代理缓冲, 输出实时到达前端。
    脚本调用 page_login 时会挂起等待用户在实时画面扫码/填表,
    前端通过 /run/{run_id}/login 轮询获取登录请求并提交答案。
    """
    stream = _stream(request)
    run_id = (req.run_id or "").strip() or uuid.uuid4().hex[:12]
    login_gate = _run_login(request).new_gate(run_id, BrowserBridge(stream))
    queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
    main_loop = asyncio.get_running_loop()
    login_gate._main_loop = main_loop

    # 用户代码在 worker 线程事件循环中执行, 阻塞调用不会拖垮主事件循环;
    # on_output 由 worker 线程回调, 通过 call_soon_threadsafe 安全投递到本循环。
    def _on_output(s: str) -> None:
        main_loop.call_soon_threadsafe(queue.put_nowait, (int(time.time() * 1000), s))

    run_task = asyncio.create_task(
        asyncio.to_thread(_run_code_worker, stream, req.code, login_gate, _on_output)
    )

    async def event_stream():
        get_task: asyncio.Task | None = None
        try:
            yield _sse_event({"type": "start", "run_id": run_id, "ts": int(time.time() * 1000)})
            get_task = asyncio.ensure_future(queue.get())
            while True:
                done_set, _ = await asyncio.wait(
                    {get_task, run_task},
                    timeout=_HEARTBEAT_INTERVAL,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if run_task in done_set:
                    # 运行完成: 立即冲刷输出并发送 done, 不必再等下一个心跳周期
                    if get_task in done_set:
                        try:
                            ts, data = get_task.result()
                            yield _sse_event({"type": "stdout", "data": data, "ts": ts})
                        except asyncio.CancelledError:
                            pass
                    elif not get_task.done():
                        get_task.cancel()
                    for _ in range(3):
                        await asyncio.sleep(0)
                        while not queue.empty():
                            ts, data = queue.get_nowait()
                            yield _sse_event({"type": "stdout", "data": data, "ts": ts})
                    try:
                        result = run_task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        result = {
                            "ok": False,
                            "output": "",
                            "error": f"{type(exc).__name__}: {exc}",
                            "saved": [],
                        }
                    yield _sse_event({"type": "done", "result": result, "ts": int(time.time() * 1000)})
                    return
                if get_task in done_set:
                    ts, data = get_task.result()
                    yield _sse_event({"type": "stdout", "data": data, "ts": ts})
                    get_task = asyncio.ensure_future(queue.get())
                    continue
                # 队列与运行都无进展: 发送空闲心跳, 防止前端/代理等待超时
                yield _sse_event({"type": "heartbeat", "ts": int(time.time() * 1000)})
        finally:
            if not run_task.done():
                run_task.cancel()
            if get_task is not None and not get_task.done():
                get_task.cancel()
            _run_login(request).remove(run_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/run/{run_id}/login", response_model=RunLoginResult)
async def run_login_status(request: Request, run_id: str) -> dict:
    """轮询当前运行的登录请求: 有则返回登录载荷, 无则返回 waiting=False。"""
    gate = _run_login(request).get_gate(run_id)
    if gate is None:
        return {"run_id": run_id, "waiting": False, "request": None}
    payload = gate.payload()
    if payload is None:
        return {"run_id": run_id, "waiting": False, "request": None}
    return {
        "run_id": run_id,
        "waiting": True,
        "request": {
            "qid": payload.get("qid", ""),
            "login_type": payload.get("login_type", "account"),
            "method": payload.get("method"),
            "url": payload.get("url"),
            "zoom_browser": payload.get("zoom_browser"),
            "message": payload.get("message"),
            "timeout": payload.get("timeout"),
            "fields": payload.get("fields") or [],
            "captcha": payload.get("captcha") or {"type": "none"},
            "submit_label": payload.get("submit_label") or "登录",
        },
    }


@router.post("/run/{run_id}/login-answer", response_model=RunLoginAnswerResult)
async def run_login_answer(request: Request, run_id: str, req: RunLoginAnswerRequest) -> dict:
    """提交独立运行的登录答案, 恢复被 page_login 挂起的脚本。"""
    gate = _run_login(request).get_gate(run_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="运行不存在或已结束")
    try:
        gate.answer(req.answers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/run/{run_id}/login-action", response_model=RunLoginAnswerResult)
async def run_login_action(request: Request, run_id: str, req: RunLoginActionRequest) -> dict:
    """独立运行登录框内的浏览器动作(发送验证码 / 刷新图形验证码)。"""
    gate = _run_login(request).get_gate(run_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="运行不存在或已结束")
    if req.action == "send_code":
        result = await gate.send_code()
    elif req.action == "refresh_captcha":
        result = await gate.refresh_captcha()
    else:
        raise HTTPException(status_code=400, detail=f"未知登录动作: {req.action}")
    return {"ok": result.get("ok", False), "message": result.get("message", "")}


@router.post("/format", response_model=FormatResult)
async def format_code(request: Request, req: FormatRequest) -> dict:
    """使用 black 格式化 Python 代码"""
    try:
        import black
    except ImportError:
        return {"ok": False, "formatted": req.code, "error": "后端缺少 black 依赖, 请执行 uv sync 安装"}
    try:
        formatted = await asyncio.to_thread(black.format_str, req.code, mode=black.Mode())
    except black.NothingChanged:  # pragma: no cover - black>=24 不再抛出该异常
        return {"ok": True, "formatted": req.code, "error": ""}
    except Exception as exc:  # SyntaxError 等
        return {"ok": False, "formatted": req.code, "error": f"格式化失败: {exc}"}
    return {"ok": True, "formatted": formatted, "error": ""}


@router.post("/organize-imports", response_model=FormatResult)
async def organize_imports(request: Request, req: FormatRequest) -> dict:
    """使用 isort 排序/分组 Python 导入语句"""
    try:
        import isort
    except ImportError:
        return {"ok": False, "formatted": req.code, "error": "后端缺少 isort 依赖, 请执行 uv sync 安装"}
    try:
        formatted = await asyncio.to_thread(isort.code, req.code)
    except Exception as exc:  # SyntaxError 等
        return {"ok": False, "formatted": req.code, "error": f"整理导入失败: {exc}"}
    return {"ok": True, "formatted": formatted, "error": ""}


@router.post("/restart", response_model=StatusResult)
async def restart(request: Request) -> dict:
    stream = _stream(request)
    await stream.restart()
    return await stream.status()


@router.post("/console/eval", response_model=EvalResult)
async def console_eval(request: Request, req: EvalRequest) -> dict:
    """在浏览器活动页面中执行 JS(DevTools Console 求值)"""
    return await _stream(request).cdp.evaluate(req.expression)


@router.post("/console/properties", response_model=PropertiesResult)
async def console_properties(request: Request, req: PropertiesRequest) -> dict:
    """展开对象: 获取指定 objectId 的属性列表"""
    return await _stream(request).cdp.get_properties(req.object_id)


@router.post("/network/body")
async def network_body(request: Request, req: NetworkBodyRequest) -> dict:
    """获取网络请求的响应体"""
    return await _stream(request).network.body(req.request_id)


@router.post("/network/clear")
async def network_clear(request: Request) -> dict:
    """清空网络记录"""
    return await _stream(request).network.clear()


@router.post("/dom/tree")
async def dom_tree(request: Request) -> dict:
    """获取当前页面整棵 DOM 树"""
    return await _stream(request).dom.tree()


@router.post("/dom/box")
async def dom_box(request: Request, req: DomBoxRequest) -> dict:
    """获取元素盒模型(视口 CSS 像素)"""
    return await _stream(request).dom.box_model(req.backend_node_id)


@router.post("/storage/origin")
async def storage_origin(request: Request) -> dict:
    """获取当前页面 origin"""
    return await _stream(request).storage.origin()


@router.post("/storage/items")
async def storage_items(request: Request, req: StorageItemsRequest) -> dict:
    """获取 Local/Session Storage 条目"""
    return await _stream(request).storage.items(req.origin, req.session)


@router.post("/storage/set")
async def storage_set(request: Request, req: StorageSetRequest) -> dict:
    """写入 Storage 条目"""
    return await _stream(request).storage.set_item(req.origin, req.session, req.key, req.value)


@router.post("/storage/remove")
async def storage_remove(request: Request, req: StorageSetRequest) -> dict:
    """删除 Storage 条目"""
    return await _stream(request).storage.remove_item(req.origin, req.session, req.key)


@router.post("/storage/cookies")
async def storage_cookies(request: Request, req: StorageItemsRequest) -> dict:
    """获取指定 origin 的 Cookies"""
    return await _stream(request).storage.cookies(req.origin)


@router.post("/storage/cookie/set")
async def storage_cookie_set(request: Request, req: CookieSetRequest) -> dict:
    """设置 Cookie"""
    return await _stream(request).storage.set_cookie(
        req.origin, req.name, req.value, path=req.path,
        domain=req.domain, http_only=req.http_only, secure=req.secure,
    )


@router.post("/storage/cookie/delete")
async def storage_cookie_delete(request: Request, req: CookieDeleteRequest) -> dict:
    """删除 Cookie"""
    return await _stream(request).storage.delete_cookie(req.origin, req.name)


@router.post("/storage/idb/databases")
async def storage_idb_databases(request: Request, req: StorageItemsRequest) -> dict:
    """列出 IndexedDB 数据库"""
    return await _stream(request).storage.idb_databases(req.origin)


@router.post("/storage/idb/stores")
async def storage_idb_stores(request: Request, req: IdbStoresRequest) -> dict:
    """列出数据库的对象仓库"""
    return await _stream(request).storage.idb_stores(req.origin, req.database)


@router.post("/storage/idb/data")
async def storage_idb_data(request: Request, req: IdbDataRequest) -> dict:
    """读取对象仓库数据"""
    return await _stream(request).storage.idb_data(
        req.origin, req.database, req.store, skip=req.skip, count=req.count)
