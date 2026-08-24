"""基于 MongoDB 的异步 LangGraph Checkpointer。

模仿 yfkm3/backend/app/agents/checkpointer.py 中的 AsyncMongoDBSaver:
- 异步方法(aget_tuple / alist / aput / aput_writes / adelete_thread)使用
  **motor**(AsyncIOMotorClient) 原生异步驱动实现, 真正异步非阻塞,
  而不是 langchain 默认 saver 通过 run_in_executor 把同步操作塞进线程池。
- 同步方法(get_tuple / list / put / put_writes / delete_thread)使用 pymongo 的
  同步 MongoClient, 供 LangGraph 同步调用路径使用。

检查点按 thread_id(= 会话 id) 隔离, 每个会话的完整对话状态持久化在
MongoDB(checkpoints / checkpoint_writes 集合), 后端重启后 Agent 可从断点恢复。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
)
from langgraph.checkpoint.mongodb.utils import dumps_metadata, loads_metadata
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pymongo import ASCENDING, MongoClient, UpdateOne

logger = logging.getLogger(__name__)

CHECKPOINT_COLLECTION = "checkpoints"
WRITES_COLLECTION = "checkpoint_writes"

_COMPOUND_INDEX = [("thread_id", 1), ("checkpoint_ns", 1), ("checkpoint_id", -1)]
_WRITES_INDEX = [
    ("thread_id", 1),
    ("checkpoint_ns", 1),
    ("checkpoint_id", -1),
    ("task_id", 1),
    ("idx", 1),
]


async def _create_saver_indexes_async(
    collection: AsyncIOMotorCollection,
    compound_index: list[tuple[str, int]],
    ttl: int | None = None,
) -> None:
    """异步创建 saver 集合的索引(基于 motor, 不阻塞事件循环)。"""

    def index_key_list(index: Any) -> list[tuple[str, int]]:
        return [(k, v) for k, v in index["key"].items()]

    indexes = await collection.list_indexes().to_list(None)
    index_keys = [index_key_list(idx) for idx in indexes]
    if compound_index not in index_keys:
        await collection.create_index(compound_index, unique=True)
    if ttl is not None:
        ttl_index = [("created_at", ASCENDING)]
        found = False
        for idx in indexes:
            if index_key_list(idx) == ttl_index and idx.get("expireAfterSeconds") == ttl:
                found = True
                break
        if not found:
            await collection.create_index(ttl_index, expireAfterSeconds=ttl)


class AsyncMongoDBSaver(BaseCheckpointSaver):
    """在 MongoDB 中存储 LangGraph 检查点的 saver。

    - ``client``: pymongo 同步 MongoClient(同步方法使用)
    - ``async_client``: motor AsyncIOMotorClient(异步方法使用, 真正异步)
    """

    client: MongoClient
    async_client: AsyncIOMotorClient
    db: Any
    async_db: Any
    checkpoint_collection: Any
    writes_collection: Any
    async_checkpoint_collection: AsyncIOMotorCollection
    async_writes_collection: AsyncIOMotorCollection

    def __init__(
        self,
        client: MongoClient,
        async_client: AsyncIOMotorClient,
        db_name: str = "crawler",
        checkpoint_collection_name: str = CHECKPOINT_COLLECTION,
        writes_collection_name: str = WRITES_COLLECTION,
        ttl: int | None = None,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde or JsonPlusSerializer())
        self.client = client
        self.async_client = async_client
        self.db = client[db_name]
        self.async_db = async_client[db_name]
        self.checkpoint_collection = self.db[checkpoint_collection_name]
        self.writes_collection = self.db[writes_collection_name]
        self.async_checkpoint_collection = self.async_db[checkpoint_collection_name]
        self.async_writes_collection = self.async_db[writes_collection_name]
        self.ttl = ttl
        self._setup_done = False

    async def setup_indexes(self) -> None:
        """异步创建集合索引(启动时调用一次)。"""
        if self._setup_done:
            return
        await _create_saver_indexes_async(
            self.async_checkpoint_collection, _COMPOUND_INDEX, self.ttl
        )
        await _create_saver_indexes_async(
            self.async_writes_collection, _WRITES_INDEX, self.ttl
        )
        self._setup_done = True

    def close(self) -> None:
        self.client.close()

    async def aclose(self) -> None:
        self.client.close()
        self.async_client.close()

    # ----- 同步方法(使用 pymongo 同步 MongoClient) -----

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        if checkpoint_id := get_checkpoint_id(config):
            query = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        else:
            query = {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}

        result = self.checkpoint_collection.find(
            query, sort=[("checkpoint_id", -1)], limit=1
        )
        for doc in result:
            config_values = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": doc["checkpoint_id"],
            }
            checkpoint = self.serde.loads_typed((doc["type"], doc["checkpoint"]))
            serialized_writes = self.writes_collection.find(config_values)
            pending_writes = [
                (
                    w["task_id"],
                    w["channel"],
                    self.serde.loads_typed((w["type"], w["value"])),
                )
                for w in serialized_writes
            ]
            return CheckpointTuple(
                {"configurable": config_values},
                checkpoint,
                loads_metadata(self.serde, doc["metadata"]),
                (
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": doc["parent_checkpoint_id"],
                        }
                    }
                    if doc.get("parent_checkpoint_id")
                    else None
                ),
                pending_writes,
            )
        return None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        query: dict[str, Any] = {}
        if config is not None:
            if "thread_id" in config["configurable"]:
                query["thread_id"] = config["configurable"]["thread_id"]
            if "checkpoint_ns" in config["configurable"]:
                query["checkpoint_ns"] = config["configurable"]["checkpoint_ns"]

        if filter:
            for key, value in filter.items():
                query[f"metadata.{key}"] = dumps_metadata(self.serde, value)

        if before is not None:
            query["checkpoint_id"] = {"$lt": before["configurable"]["checkpoint_id"]}

        result = self.checkpoint_collection.find(
            query, limit=0 if limit is None else limit, sort=[("checkpoint_id", -1)]
        )

        for doc in result:
            config_values = {
                "thread_id": doc["thread_id"],
                "checkpoint_ns": doc["checkpoint_ns"],
                "checkpoint_id": doc["checkpoint_id"],
            }
            serialized_writes = self.writes_collection.find(config_values)
            pending_writes = [
                (
                    w["task_id"],
                    w["channel"],
                    self.serde.loads_typed((w["type"], w["value"])),
                )
                for w in serialized_writes
            ]
            yield CheckpointTuple(
                config={"configurable": config_values},
                checkpoint=self.serde.loads_typed((doc["type"], doc["checkpoint"])),
                metadata=loads_metadata(self.serde, doc["metadata"]),
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": doc["thread_id"],
                            "checkpoint_ns": doc["checkpoint_ns"],
                            "checkpoint_id": doc["parent_checkpoint_id"],
                        }
                    }
                    if doc.get("parent_checkpoint_id")
                    else None
                ),
                pending_writes=pending_writes,
            )

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = checkpoint["id"]
        type_, serialized_checkpoint = self.serde.dumps_typed(checkpoint)
        metadata = metadata.copy()
        metadata.update(config.get("metadata", {}))
        doc = {
            "parent_checkpoint_id": config["configurable"].get("checkpoint_id"),
            "type": type_,
            "checkpoint": serialized_checkpoint,
            "metadata": dumps_metadata(self.serde, metadata),
        }
        upsert_query = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
        if self.ttl:
            doc["created_at"] = datetime.now(tz=UTC)

        self.checkpoint_collection.update_one(upsert_query, {"$set": doc}, upsert=True)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = config["configurable"]["checkpoint_id"]
        set_method = "$set" if all(w[0] in WRITES_IDX_MAP for w in writes) else "$setOnInsert"
        operations = []
        now = datetime.now(tz=UTC)
        for idx, (channel, value) in enumerate(writes):
            upsert_query = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "task_path": task_path,
                "idx": WRITES_IDX_MAP.get(channel, idx),
            }
            type_, serialized_value = self.serde.dumps_typed(value)
            update_doc: dict[str, Any] = {
                "channel": channel,
                "type": type_,
                "value": serialized_value,
            }
            if self.ttl:
                update_doc["created_at"] = now
            operations.append(
                UpdateOne(
                    filter=upsert_query,
                    update={set_method: update_doc},
                    upsert=True,
                )
            )
        self.writes_collection.bulk_write(operations)

    def delete_thread(self, thread_id: str) -> None:
        self.checkpoint_collection.delete_many({"thread_id": thread_id})
        self.writes_collection.delete_many({"thread_id": thread_id})

    # ----- 异步方法(使用 motor, 真正异步) -----

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        if checkpoint_id := get_checkpoint_id(config):
            query = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        else:
            query = {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}

        docs = await self.async_checkpoint_collection.find(
            query, sort=[("checkpoint_id", -1)], limit=1
        ).to_list(1)

        for doc in docs:
            config_values = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": doc["checkpoint_id"],
            }
            checkpoint = self.serde.loads_typed((doc["type"], doc["checkpoint"]))
            serialized_writes = await self.async_writes_collection.find(
                config_values
            ).to_list(None)
            pending_writes = [
                (
                    w["task_id"],
                    w["channel"],
                    self.serde.loads_typed((w["type"], w["value"])),
                )
                for w in serialized_writes
            ]
            return CheckpointTuple(
                {"configurable": config_values},
                checkpoint,
                loads_metadata(self.serde, doc["metadata"]),
                (
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": doc["parent_checkpoint_id"],
                        }
                    }
                    if doc.get("parent_checkpoint_id")
                    else None
                ),
                pending_writes,
            )
        return None

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        query: dict[str, Any] = {}
        if config is not None:
            if "thread_id" in config["configurable"]:
                query["thread_id"] = config["configurable"]["thread_id"]
            if "checkpoint_ns" in config["configurable"]:
                query["checkpoint_ns"] = config["configurable"]["checkpoint_ns"]

        if filter:
            for key, value in filter.items():
                query[f"metadata.{key}"] = dumps_metadata(self.serde, value)

        if before is not None:
            query["checkpoint_id"] = {"$lt": before["configurable"]["checkpoint_id"]}

        cursor = self.async_checkpoint_collection.find(
            query, limit=0 if limit is None else limit, sort=[("checkpoint_id", -1)]
        )

        async for doc in cursor:
            config_values = {
                "thread_id": doc["thread_id"],
                "checkpoint_ns": doc["checkpoint_ns"],
                "checkpoint_id": doc["checkpoint_id"],
            }
            serialized_writes = await self.async_writes_collection.find(
                config_values
            ).to_list(None)
            pending_writes = [
                (
                    w["task_id"],
                    w["channel"],
                    self.serde.loads_typed((w["type"], w["value"])),
                )
                for w in serialized_writes
            ]
            yield CheckpointTuple(
                config={"configurable": config_values},
                checkpoint=self.serde.loads_typed((doc["type"], doc["checkpoint"])),
                metadata=loads_metadata(self.serde, doc["metadata"]),
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": doc["thread_id"],
                            "checkpoint_ns": doc["checkpoint_ns"],
                            "checkpoint_id": doc["parent_checkpoint_id"],
                        }
                    }
                    if doc.get("parent_checkpoint_id")
                    else None
                ),
                pending_writes=pending_writes,
            )

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = checkpoint["id"]
        type_, serialized_checkpoint = self.serde.dumps_typed(checkpoint)
        metadata = metadata.copy()
        metadata.update(config.get("metadata", {}))
        doc = {
            "parent_checkpoint_id": config["configurable"].get("checkpoint_id"),
            "type": type_,
            "checkpoint": serialized_checkpoint,
            "metadata": dumps_metadata(self.serde, metadata),
        }
        upsert_query = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
        if self.ttl:
            doc["created_at"] = datetime.now(tz=UTC)

        await self.async_checkpoint_collection.update_one(
            upsert_query, {"$set": doc}, upsert=True
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = config["configurable"]["checkpoint_id"]
        set_method = "$set" if all(w[0] in WRITES_IDX_MAP for w in writes) else "$setOnInsert"
        operations = []
        now = datetime.now(tz=UTC)
        for idx, (channel, value) in enumerate(writes):
            upsert_query = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "task_path": task_path,
                "idx": WRITES_IDX_MAP.get(channel, idx),
            }
            type_, serialized_value = self.serde.dumps_typed(value)
            update_doc: dict[str, Any] = {
                "channel": channel,
                "type": type_,
                "value": serialized_value,
            }
            if self.ttl:
                update_doc["created_at"] = now
            operations.append(
                UpdateOne(
                    filter=upsert_query,
                    update={set_method: update_doc},
                    upsert=True,
                )
            )
        await self.async_writes_collection.bulk_write(operations)

    async def adelete_thread(self, thread_id: str) -> None:
        await self.async_checkpoint_collection.delete_many({"thread_id": thread_id})
        await self.async_writes_collection.delete_many({"thread_id": thread_id})


class CheckpointerFactory:
    """管理共享的 MongoDB 客户端与 AsyncMongoDBSaver 单例。"""

    _saver: AsyncMongoDBSaver | None = None
    _client: MongoClient | None = None
    _async_client: AsyncIOMotorClient | None = None

    @classmethod
    def get_saver(
        cls, mongo_uri: str, mongo_db: str, serde: SerializerProtocol | None = None
    ) -> AsyncMongoDBSaver:
        if cls._saver is not None:
            return cls._saver
        if not mongo_uri:
            raise ValueError("MongoDB URL 未配置(--mongo-uri / MONGO_URI)")
        cls._client = MongoClient(mongo_uri)
        cls._async_client = AsyncIOMotorClient(mongo_uri)
        cls._saver = AsyncMongoDBSaver(
            client=cls._client,
            async_client=cls._async_client,
            db_name=mongo_db,
            serde=serde or JsonPlusSerializer(),
        )
        logger.info("[Checkpointer] Created motor-based AsyncMongoDBSaver")
        return cls._saver

    @classmethod
    async def setup(
        cls, mongo_uri: str, mongo_db: str, serde: SerializerProtocol | None = None
    ) -> AsyncMongoDBSaver:
        saver = cls.get_saver(mongo_uri, mongo_db, serde=serde)
        try:
            await saver.setup_indexes()
        except Exception as exc:  # noqa: BLE001  MongoDB 不可达时降级, 不阻塞应用启动
            logger.warning("[Checkpointer] 索引创建失败(将延迟到首次使用): %s", exc)
        return saver

    @classmethod
    async def close(cls) -> None:
        if cls._client is not None:
            cls._client.close()
        if cls._async_client is not None:
            cls._async_client.close()
        cls._saver = None
        cls._client = None
        cls._async_client = None


def get_checkpointer(cfg: Any) -> AsyncMongoDBSaver:
    """获取(或惰性创建)共享的异步 MongoDB checkpointer。"""
    return CheckpointerFactory.get_saver(cfg.mongo_uri, cfg.mongo_db)


async def setup_checkpointer(cfg: Any) -> None:
    """应用启动时初始化 checkpointer 并异步创建集合索引。"""
    await CheckpointerFactory.setup(cfg.mongo_uri, cfg.mongo_db)


async def close_checkpointer() -> None:
    """应用关闭时释放 checkpointer 资源。"""
    await CheckpointerFactory.close()