"""代码版本路由: 仓库状态 / 提交 / 历史 / 检出(按 crawler_id 隔离)。

未提交变更暂存在浏览器(localStorage), 本路由只负责已提交快照(commit)与 HEAD:
- GET  /code/repo               仓库状态(HEAD 摘要)
- POST /code/commit             把工作区内容固化为一次提交
- GET  /code/commits            历史列表(倒序, 含相对父提交的 stat)
- GET  /code/commits/{id}       单次提交详情(含全量源码)
- POST /code/checkout           检出某次提交内容到工作区(不动 HEAD)

crawler_id 缺省回退后端配置, 再回退 "default"(与 Agent 会话一致)。
MongoDB 不可达: 读接口优雅降级(空列表 / head=null), 写接口返回 503。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..schemas import (
    CodeCheckoutRequest,
    CodeCheckoutResult,
    CodeCommitInfo,
    CodeCommitListResult,
    CodeCommitRequest,
    CodeCommitResult,
    CodeRepoResult,
)
from ..services.code_version import CodeStore, NoChangeError

router = APIRouter(tags=["code"])


def _cid(request: Request, value: str | None) -> str:
    cfg = request.app.state.cfg
    base = (getattr(cfg, "crawler_id", None) or "").strip()
    return (value or base).strip() or "default"


def _store(request: Request) -> CodeStore:
    store = getattr(request.app.state, "code_store", None)
    if store is not None:
        return store
    cfg = request.app.state.cfg
    store = CodeStore(
        getattr(cfg, "mongo_uri", "") or "",
        getattr(cfg, "mongo_db", "crawler"),
    )
    request.app.state.code_store = store
    return store


@router.get("/code/repo", response_model=CodeRepoResult)
async def code_repo(request: Request) -> dict:
    """查询仓库状态: 是否有提交 + HEAD 摘要。"""
    cid = _cid(request, request.query_params.get("crawler_id"))
    try:
        repo = await _store(request).get_repo(cid)
        has = False
        head = None
        if repo and repo.get("head"):
            head_doc = await _store(request).get_commit(cid, repo["head"])
            if head_doc:
                has = True
                head = {
                    "commit_id": head_doc["commit_id"],
                    "message": head_doc["message"],
                    "created_at": head_doc["created_at"],
                }
        return {"crawler_id": cid, "has_commits": has, "head": head}
    except ConnectionError:
        return {"crawler_id": cid, "has_commits": False, "head": None}


@router.post("/code/commit", response_model=CodeCommitResult)
async def code_commit(request: Request, req: CodeCommitRequest) -> dict:
    """把工作区内容固化为一次提交, HEAD 前移到新提交。"""
    cid = _cid(request, req.crawler_id)
    try:
        commit = await _store(request).create_commit(
            cid, req.message, req.content, req.author
        )
    except NoChangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB 不可达: {exc}") from exc
    return {"ok": True, "commit": commit}


@router.get("/code/commits", response_model=CodeCommitListResult)
async def code_commits(request: Request) -> dict:
    """历史列表(按 created_at 倒序, 默认 50 条, 可选 before 分页)。"""
    cid = _cid(request, request.query_params.get("crawler_id"))
    try:
        limit = int(request.query_params.get("limit", "50"))
    except (TypeError, ValueError):
        limit = 50
    before = request.query_params.get("before") or None
    try:
        commits = await _store(request).list_commits(cid, limit=limit, before=before)
    except ConnectionError:
        commits = []
    return {"commits": commits}


@router.get("/code/commits/{commit_id}", response_model=CodeCommitInfo)
async def code_commit_detail(request: Request, commit_id: str) -> dict:
    """单次提交详情(含全量源码, 供检出/对比)。"""
    cid = _cid(request, request.query_params.get("crawler_id"))
    try:
        doc = await _store(request).get_commit(cid, commit_id)
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB 不可达: {exc}") from exc
    if doc is None:
        raise HTTPException(status_code=404, detail="提交不存在")
    return doc


@router.post("/code/checkout", response_model=CodeCheckoutResult)
async def code_checkout(request: Request, req: CodeCheckoutRequest) -> dict:
    """把指定提交的内容检出到工作区(不动 HEAD)。"""
    cid = _cid(request, req.crawler_id)
    try:
        doc = await _store(request).get_commit(cid, req.commit_id)
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB 不可达: {exc}") from exc
    if doc is None:
        raise HTTPException(status_code=404, detail="提交不存在")
    return {"ok": True, "commit_id": doc["commit_id"], "code": doc["content"]}