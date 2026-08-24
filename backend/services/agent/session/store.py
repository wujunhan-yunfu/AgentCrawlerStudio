"""Agent 会话与消息的 MongoDB 持久化(按 crawler_id 隔离, 全异步)。

- agent_sessions:  会话元信息(标题/类型/状态/更新时间等), 索引 crawler_id + updated_at
- agent_messages:  会话内消息(user/assistant/event), 索引 session_id + ts

客户端按 crawler_id 读写, 不同爬虫实例的会话与消息互相隔离。
"""

from __future__ import annotations

import time
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING

SESSION_COLLECTION = "agent_sessions"
MESSAGE_COLLECTION = "agent_messages"

# 单次连接/操作的最大等待时间: MongoDB 不可达时快速失败, 而不是默认 30s 的
# serverSelectionTimeout。连接失败后进入冷却期, 冷却期内直接失败不再尝试,
# 避免关键路径上每次 store 调用都空等超时(原实现每次调用空等 3s)。
_SELECT_TIMEOUT_MS = 1000
_CONNECT_TIMEOUT_MS = 1000
_RETRY_COOLDOWN_S = 2.0


class AgentStore:
    """会话/消息存储, Mongo 连接惰性建立(基于异步驱动 motor)。

    连接带快速失败 + 冷却: MongoDB 不可用时, 首次操作最多等待
    ``_SELECT_TIMEOUT_MS`` 即抛出, 随后 ``_RETRY_COOLDOWN_S`` 秒内直接失败,
    避免多次串行操作各自空等超时。
    """

    def __init__(self, mongo_uri: str, mongo_db: str) -> None:
        self._mongo_uri = mongo_uri
        self._mongo_db_name = mongo_db
        self._client: AsyncIOMotorClient | None = None
        self._sessions: Any = None
        self._messages: Any = None
        self._down_until: float = 0.0

    async def _connect(self) -> None:
        if self._sessions is not None:
            return
        now = time.monotonic()
        if self._down_until > now:
            raise ConnectionError("MongoDB 暂不可用(冷却中)")
        try:
            self._client = AsyncIOMotorClient(
                self._mongo_uri,
                serverSelectionTimeoutMS=_SELECT_TIMEOUT_MS,
                connectTimeoutMS=_CONNECT_TIMEOUT_MS,
            )
            db = self._client[self._mongo_db_name]
            self._sessions = db[SESSION_COLLECTION]
            self._messages = db[MESSAGE_COLLECTION]
            await self._sessions.create_index([("session_id", ASCENDING)], unique=True)
            await self._sessions.create_index(
                [("crawler_id", ASCENDING), ("updated_at", DESCENDING)]
            )
            await self._messages.create_index(
                [("session_id", ASCENDING), ("ts", ASCENDING)]
            )
            await self._messages.create_index(
                [("crawler_id", ASCENDING), ("session_id", ASCENDING)]
            )
            self._down_until = 0.0
        except Exception:
            if self._client is not None:
                self._client.close()
                self._client = None
            self._sessions = None
            self._messages = None
            self._down_until = time.monotonic() + _RETRY_COOLDOWN_S
            raise

    # ------------------------------------------------------------ 会话

    async def create_session(
        self, session_id: str, crawler_id: str, title: str
    ) -> dict[str, Any]:
        await self._connect()
        doc = {
            "session_id": session_id,
            "crawler_id": crawler_id,
            "title": title,
            "status": "idle",
            "message_count": 0,
            "last_message": "",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        await self._sessions.insert_one(doc)
        return dict(doc)

    async def get_session(self, session_id: str, crawler_id: str) -> dict[str, Any] | None:
        await self._connect()
        doc = await self._sessions.find_one(
            {"session_id": session_id, "crawler_id": crawler_id}
        )
        return self._strip_session(doc) if doc else None

    async def list_sessions(self, crawler_id: str, limit: int = 200) -> list[dict[str, Any]]:
        await self._connect()
        cursor = (
            self._sessions.find({"crawler_id": crawler_id})
            .sort("updated_at", DESCENDING)
            .limit(limit)
        )
        docs = [doc async for doc in cursor]
        return [self._strip_session(d) for d in docs]

    async def update_session(self, session_id: str, crawler_id: str, **fields: Any) -> None:
        await self._connect()
        fields["updated_at"] = time.time()
        await self._sessions.update_one(
            {"session_id": session_id, "crawler_id": crawler_id},
            {"$set": fields},
        )

    async def delete_session(self, session_id: str, crawler_id: str) -> None:
        await self._connect()
        await self._sessions.delete_one({"session_id": session_id, "crawler_id": crawler_id})
        await self._messages.delete_many({"session_id": session_id, "crawler_id": crawler_id})

    # ------------------------------------------------------------ 消息

    async def add_message(
        self,
        session_id: str,
        crawler_id: str,
        role: str,
        type_: str,
        content: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._connect()
        doc = {
            "session_id": session_id,
            "crawler_id": crawler_id,
            "role": role,
            "type": type_,
            "content": content,
            "meta": meta or {},
            "ts": time.time(),
        }
        res = await self._messages.insert_one(doc)
        return {"id": str(res.inserted_id), **doc}

    async def list_messages(
        self, session_id: str, crawler_id: str, limit: int = 5000
    ) -> list[dict[str, Any]]:
        await self._connect()
        cursor = (
            self._messages.find({"session_id": session_id, "crawler_id": crawler_id})
            .sort("ts", ASCENDING)
            .limit(limit)
        )
        docs = [doc async for doc in cursor]
        return [self._strip(d) for d in docs]

    async def count_messages(self, session_id: str, crawler_id: str) -> int:
        await self._connect()
        return await self._messages.count_documents(
            {"session_id": session_id, "crawler_id": crawler_id}
        )

    # ------------------------------------------------------------ 工具

    @staticmethod
    def _strip(doc: dict[str, Any]) -> dict[str, Any]:
        doc = dict(doc)
        doc["id"] = str(doc.pop("_id"))
        return doc

    @staticmethod
    def _strip_session(doc: dict[str, Any]) -> dict[str, Any]:
        """会话文档: 以 session_id 作为对外 id(与事件/接口使用的会话标识保持一致)。

        同时移除 _id(ObjectId, 无法 JSON 序列化), 避免 WebSocket hello 推送时序列化失败。
        """
        doc = dict(doc)
        doc.pop("_id", None)
        doc["id"] = doc["session_id"]
        return doc
