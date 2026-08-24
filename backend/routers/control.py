"""控制接口路由: 状态/页面/导航/截图/运行代码/重启"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Request, Response

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
    RunResult,
    StatusResult,
    StorageItemsRequest,
    StorageSetRequest,
)
from ..services.agent.bridge import BrowserBridge
from ..services.agent.run_login import RunLoginManager
from ..services.browser import BrowserError

router = APIRouter(tags=["control"])


def _stream(request: Request):
    return request.app.state.stream


def _run_login(request: Request) -> RunLoginManager:
    return request.app.state.run_login


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


@router.post("/run", response_model=RunResult)
async def run(request: Request, req: RunRequest) -> dict:
    """执行 Playwright 代码: 每次自动重启全新无痕浏览器。

    脚本调用 page_login 时会挂起等待用户在实时画面扫码/填表,
    前端通过 /run/{run_id}/login 轮询获取登录请求并提交答案。
    """
    stream = _stream(request)
    run_id = (req.run_id or "").strip() or uuid.uuid4().hex[:12]
    login_gate = _run_login(request).new_gate(run_id, BrowserBridge(stream))
    try:
        return await stream.run_code(req.code, login_gate=login_gate)
    finally:
        _run_login(request).remove(run_id)


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
