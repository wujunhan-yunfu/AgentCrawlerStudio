"""代码版本管理: 未提交变更暂存在浏览器, 已提交快照与仓库 HEAD 存 MongoDB(按 crawler_id 隔离)。

- code_commits: 不可变提交快照。commit_id = sha1(crawler_id + parent + content + message + created_at),
  内容重复/内容哈希判重保证同一 crawler 内提交链线性且无重复提交。
- code_repos:   每个 crawler 一条, 记录当前 HEAD commit_id(空仓库为 null)。

连接采用 motor 异步驱动 + 快速失败/冷却: MongoDB 不可达时首次操作最多等待
``_SELECT_TIMEOUT_MS`` 即抛出, 随后 ``_RETRY_COOLDOWN_S`` 秒内直接失败,
避免关键路径每次调用空等超时(与 backend/services/agent/session/store.py 一致)。
"""

from __future__ import annotations

import hashlib
import time
from difflib import SequenceMatcher
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING

COMMIT_COLLECTION = "code_commits"
REPO_COLLECTION = "code_repos"

_SELECT_TIMEOUT_MS = 1000
_CONNECT_TIMEOUT_MS = 1000
_RETRY_COOLDOWN_S = 2.0


class NoChangeError(Exception):
    """提交内容与 HEAD 一致, 无可提交变更。"""


def content_hash(content: str) -> str:
    """sha1(content), 用于快速判重/判脏。"""
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def commit_hash(
    crawler_id: str,
    parent: str | None,
    content: str,
    message: str,
    created_at: int,
) -> str:
    """sha1(crawler_id + parent + content + message + created_at), 保证提交标识唯一且确定。"""
    raw = f"{crawler_id}\0{parent or ''}\0{content}\0{message}\0{created_at}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def diff_stat(old: str, new: str) -> dict[str, int]:
    """相对父提交的变更统计(行级): 首提交统计全部行。"""
    a = (old or "").splitlines()
    b = (new or "").splitlines()
    if not a:
        return {"add": len(b), "del": 0}
    add = 0
    del_ = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=a, b=b).get_opcodes():
        if tag in ("insert", "replace"):
            add += j2 - j1
        if tag in ("delete", "replace"):
            del_ += i2 - i1
    return {"add": add, "del": del_}


class CodeStore:
    """提交快照与仓库 HEAD 的 Mongo 存储(motor 异步 + 快速失败/冷却)。

    MongoDB 不可达时: 读接口由路由层降级(空列表 / head=null), 写接口返回 503。
    """

    def __init__(self, mongo_uri: str, mongo_db: str) -> None:
        self._mongo_uri = mongo_uri
        self._mongo_db_name = mongo_db
        self._client: AsyncIOMotorClient | None = None
        self._commits: Any = None
        self._repos: Any = None
        self._down_until: float = 0.0

    async def _connect(self) -> None:
        if self._commits is not None:
            return
        now = time.monotonic()
        if self._down_until > now:
            raise ConnectionError("MongoDB 暂不可用(冷却中)")
        if not self._mongo_uri:
            raise ConnectionError("MongoDB URL 未配置(--mongo-uri / MONGO_URI)")
        try:
            self._client = AsyncIOMotorClient(
                self._mongo_uri,
                serverSelectionTimeoutMS=_SELECT_TIMEOUT_MS,
                connectTimeoutMS=_CONNECT_TIMEOUT_MS,
            )
            db = self._client[self._mongo_db_name]
            self._commits = db[COMMIT_COLLECTION]
            self._repos = db[REPO_COLLECTION]
            await self._commits.create_index(
                [("crawler_id", ASCENDING), ("commit_id", ASCENDING)], unique=True
            )
            await self._commits.create_index(
                [("crawler_id", ASCENDING), ("created_at", DESCENDING)]
            )
            await self._repos.create_index([("crawler_id", ASCENDING)], unique=True)
            self._down_until = 0.0
        except Exception as exc:
            if self._client is not None:
                self._client.close()
                self._client = None
            self._commits = None
            self._repos = None
            self._down_until = time.monotonic() + _RETRY_COOLDOWN_S
            raise ConnectionError(f"MongoDB 不可达: {exc}") from exc

    # ------------------------------------------------------------ 仓库状态

    async def get_repo(self, crawler_id: str) -> dict[str, Any] | None:
        """当前 crawler 的仓库状态文档(含 head), 无仓库返回 None。"""
        await self._connect()
        doc = await self._repos.find_one({"crawler_id": crawler_id})
        return dict(doc) if doc else None

    async def get_head(self, crawler_id: str) -> str | None:
        """当前 HEAD commit_id, 空仓库返回 None。"""
        repo = await self.get_repo(crawler_id)
        return repo.get("head") if repo else None

    # ------------------------------------------------------------ 提交

    async def create_commit(
        self,
        crawler_id: str,
        message: str,
        content: str,
        author: str = "unknown",
    ) -> dict[str, Any]:
        """把工作区内容固化为一次提交, 并前移 HEAD。内容与 HEAD 一致时抛 NoChangeError。"""
        await self._connect()
        parent = await self.get_head(crawler_id)
        ch = content_hash(content)
        if parent is not None:
            parent_doc = await self._commits.find_one(
                {"crawler_id": crawler_id, "commit_id": parent}
            )
            if parent_doc and parent_doc.get("content_hash") == ch:
                raise NoChangeError("工作区与最新提交一致, 无变更可提交")
        created_at = int(time.time() * 1000)
        commit_id = commit_hash(crawler_id, parent, content, message, created_at)
        doc = {
            "crawler_id": crawler_id,
            "commit_id": commit_id,
            "parent": parent,
            "message": message,
            "author": author,
            "content": content,
            "content_hash": ch,
            "size": len(content.encode("utf-8")),
            "created_at": created_at,
        }
        await self._commits.insert_one(doc)
        await self._repos.update_one(
            {"crawler_id": crawler_id},
            {"$set": {"head": commit_id, "updated_at": created_at}},
            upsert=True,
        )
        return _strip(doc)

    async def get_commit(self, crawler_id: str, commit_id: str) -> dict[str, Any] | None:
        """单次提交详情(含全量 content)。"""
        await self._connect()
        doc = await self._commits.find_one(
            {"crawler_id": crawler_id, "commit_id": commit_id}
        )
        return _strip(doc) if doc else None

    async def list_commits(
        self,
        crawler_id: str,
        limit: int = 50,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """历史列表(按 created_at 倒序), 不含全量 content, 附带相对父提交的 stat。

        - before: 游标 commit_id, 返回该提交之前的历史(用于分页)。
        """
        await self._connect()
        query: dict[str, Any] = {"crawler_id": crawler_id}
        if before:
            # 游标: 以该提交的 _id(插入顺序) 为界, 与 created_at 倒序保持一致
            cursor_doc = await self._commits.find_one(
                {"crawler_id": crawler_id, "commit_id": before}
            )
            if cursor_doc:
                query["_id"] = {"$lt": cursor_doc["_id"]}
        cursor = (
            self._commits.find(query)
            .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
            .limit(max(1, min(limit, 200)))
        )
        docs = [doc async for doc in cursor]

        parent_ids = {d.get("parent") for d in docs if d.get("parent")}
        parent_content: dict[str, str] = {}
        if parent_ids:
            pcur = self._commits.find(
                {"crawler_id": crawler_id, "commit_id": {"$in": list(parent_ids)}}
            )
            async for pd in pcur:
                parent_content[pd["commit_id"]] = pd.get("content", "")

        out: list[dict[str, Any]] = []
        for d in docs:
            content = d.get("content", "")
            parent = d.get("parent")
            if parent:
                stat = diff_stat(parent_content.get(parent, ""), content)
            else:
                stat = {"add": len(content.splitlines()), "del": 0}
            item = _strip(d)
            item.pop("content", None)
            item["stat"] = stat
            out.append(item)
        return out


def _strip(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id", ""))
    return doc