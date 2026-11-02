"""上下文压缩策略/中间件/审计测试。

不依赖真实 LLM: 主模型与摘要模型均用 FakeListChatModel, 后端用 StateBackend。
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from types import SimpleNamespace

import pytest
from deepagents.backends import StateBackend
from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from mongomock_motor import AsyncMongoMockClient

from backend.config import Config
from backend.services.agent.core.compression import (
    CompressionPolicy,
    _cap_summary,
    _fallback_summary,
    build_compression_middleware,
    estimate_after_tokens,
    estimate_request_tokens,
)
from backend.services.agent.session.event import EventHub
from backend.services.agent.session.model import AgentSession


def make_cfg(**overrides) -> Config:
    base = Config(
        llm_api_key="test",
        llm_model="fake",
        context_window=2000,
        compress_trigger_fraction=0.5,
        compress_hard_fraction=0.9,
        compress_keep_messages=2,
        compress_trigger_messages=8,
        compress_min_history_tokens=0,
        compress_min_gap_messages=0,
        compress_min_gap_tokens=0,
        compress_summary_max_tokens=400,
        compress_chars_per_token=1.8,
    )
    return replace(base, **overrides)


def make_messages(n: int = 10, size: int = 300) -> list[HumanMessage]:
    return [HumanMessage(content=f"m{i}:" + "x" * size) for i in range(n)]


def make_middleware(cfg=None, session=None, summary_model=None, audit=None):
    cfg = cfg or make_cfg()
    session = session or AgentSession(
        id="s1", crawler_id="c", title="T", hub=EventHub()
    )
    model = FakeListChatModel(responses=["摘要内容"])
    summary_model = summary_model or model
    mw = build_compression_middleware(
        cfg,
        session,
        model,
        backend=StateBackend(),
        audit=audit,
        summary_model=summary_model,
    )
    return mw, session, model


# --------------------------------------------------------------------------- policy


def test_middleware_name_matches_deepagents_default():
    """同名才能被 create_deep_agent 原位替换, 而不是追加第二个压缩器。"""
    mw, _, _ = make_middleware()
    assert mw.name == "SummarizationMiddleware"


def test_policy_from_config_defaults():
    p = CompressionPolicy.from_config(Config())
    assert p.mode == "auto"
    assert p.trigger_tokens == int(128000 * 0.62)
    assert p.hard_tokens == int(128000 * 0.85)
    assert p.enabled is True


def test_policy_disabled_by_switch():
    p = CompressionPolicy.from_config(Config(context_compress=False))
    assert p.mode == "off"
    assert p.enabled is False


def test_policy_observe_mode():
    p = CompressionPolicy.from_config(Config(compress_mode="observe"))
    assert p.mode == "observe"
    assert p.enabled is True


def test_policy_clamps_values():
    p = CompressionPolicy.from_config(
        Config(context_window=100, compress_keep_messages=0, compress_timeout=0)
    )
    assert p.context_window >= 1024
    assert p.keep_messages >= 2
    assert p.timeout >= 1.0


def test_token_estimator_counts_chinese_lower_chars_per_token():
    text = "中文" * 200
    approx_default = CompressionPolicy(chars_per_token=4.0).estimate(
        [HumanMessage(content=text)]
    )
    approx_cn = CompressionPolicy(chars_per_token=1.8).estimate(
        [HumanMessage(content=text)]
    )
    assert approx_cn > approx_default


# --------------------------------------------------------------------------- 摘要工具


def test_cap_summary_keeps_short_text():
    assert _cap_summary("短摘要", 400, 1.8) == "短摘要"


def test_cap_summary_truncates_long_text():
    text = "行内容\n" * 1000
    out = _cap_summary(text, 100, 1.8)
    assert "摘要已按预算截断" in out
    assert len(out) < len(text)


def test_fallback_summary_keeps_goal_paths_and_next_step():
    out = _fallback_summary(
        [
            HumanMessage(content="采集 https://example.com/list 的数据"),
            ToolMessage(
                content="已保存到 /saved/items.json",
                tool_call_id="t1",
            ),
        ]
    )
    assert "采集 https://example.com/list" in out
    assert "/saved/items.json" in out
    assert "未解决问题与下一步" in out


# --------------------------------------------------------------------------- 判定


def test_trigger_and_keep_limits():
    mw, _, _ = make_middleware()
    msgs = make_messages(10)
    tokens = 2000
    assert mw._should_summarize(msgs, tokens) is True


def test_not_enough_history_does_not_trigger():
    mw, _, _ = make_middleware()
    msgs = make_messages(3)
    assert mw._should_summarize(msgs, 10_000) is False


def test_debounce_blocks_until_gap_met():
    cfg = make_cfg(compress_min_gap_messages=20, compress_min_gap_tokens=0)
    mw, session, _ = make_middleware(cfg=cfg)
    msgs = make_messages(10)
    session.last_compression_message_count = len(msgs) - 1
    # 1500 超过软阈值(1000)、低于硬阈值(1800), 受防抖约束
    assert mw._should_summarize(msgs, 1500) is False
    session.last_compression_message_count = 0
    assert mw._should_summarize(msgs, 1500) is True


def test_hard_threshold_bypasses_debounce():
    cfg = make_cfg(
        compress_min_gap_messages=20,
        compress_hard_fraction=0.6,
    )
    mw, session, _ = make_middleware(cfg=cfg)
    msgs = make_messages(10)
    session.last_compression_message_count = len(msgs)
    # 超过硬阈值 -> 忽略防抖
    assert mw._should_summarize(msgs, 10_000) is True


def test_manual_force_only_once():
    cfg = make_cfg(compress_trigger_messages=10**6, compress_trigger_fraction=0.99)
    mw, session, _ = make_middleware(cfg=cfg)
    msgs = make_messages(10)
    session.compress_requested = True
    assert mw._begin_forced_call() is True
    assert mw._should_summarize(msgs, 10) is True
    assert mw._force_next is False
    assert mw._should_summarize(msgs, 10) is False


def test_observe_mode_reports_without_applying():
    cfg = make_cfg(compress_mode="observe")
    mw, session, _ = make_middleware(cfg=cfg)
    msgs = make_messages(10)
    assert mw._should_summarize(msgs, 2000) is False
    events = [e for e in session.hub._buffer if e.get("type") == "context_compression"]
    assert events and events[-1]["applied"] is False


def test_off_mode_never_triggers():
    cfg = make_cfg(context_compress=False)
    mw, session, _ = make_middleware(cfg=cfg)
    msgs = make_messages(10)
    assert mw._should_summarize(msgs, 2000) is False
    assert not [e for e in session.hub._buffer if e.get("type") == "context_compression"]


# --------------------------------------------------------------------------- 摘要与事件


async def test_summarize_emits_start_and_uses_prompt():
    cfg = make_cfg()
    model = FakeListChatModel(responses=["## 任务目标\n采集数据"])
    mw, session, _ = make_middleware(cfg=cfg, summary_model=model)
    out = await mw._summarize([HumanMessage(content="历史内容")])
    assert "任务目标" in out
    types = [e["type"] for e in session.hub._buffer]
    assert "context_compression_start" in types


async def test_summarize_falls_back_on_model_error():
    class BoomModel:
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("boom")

    mw, session, _ = make_middleware(summary_model=BoomModel())
    out = await mw._summarize([HumanMessage(content="目标: 采集 /saved/a.json")])
    assert "任务目标" in out
    types = [e["type"] for e in session.hub._buffer]
    assert "context_compression_error" in types


async def test_awrap_model_call_compresses_and_emits_event():
    mw, session, model = make_middleware()
    msgs = make_messages(12)
    request = ModelRequest(
        model=model,
        messages=msgs,
        state={"messages": msgs},
        runtime=SimpleNamespace(context=None),
    )

    async def handler(req):
        return AIMessage(content="ok")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        resp = await mw.awrap_model_call(request, handler)

    assert isinstance(resp, ExtendedModelResponse)
    update = resp.command.update
    assert "_summarization_event" in update
    event = session.last_compression
    assert event is not None
    assert event["applied"] is True
    assert event["saved_tokens"] >= 0
    assert event["version"] == 1
    assert session.compression_count == 1
    assert session.last_compression_message_count > 0
    types = [e["type"] for e in session.hub._buffer]
    assert "context_compression" in types


async def test_summarize_retries_after_context_overflow():
    class OverflowOnce:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                from langchain_core.exceptions import ContextOverflowError

                raise ContextOverflowError("too long")
            return SimpleNamespace(content="压缩后的摘要")

    model = OverflowOnce()
    mw, _, _ = make_middleware(summary_model=model)
    out = await mw._summarize(make_messages(10))
    assert out == "压缩后的摘要"
    assert model.calls == 2


async def test_audit_failure_is_swallowed():
    async def boom(payload, summary):
        raise RuntimeError("mongo down")

    mw, session, _ = make_middleware(audit=boom)
    mw._before_tokens = 100
    request = SimpleNamespace(messages=make_messages(4), system_message=None, tools=[])
    event = {
        "cutoff_index": 2,
        "summary_message": HumanMessage(content="S"),
        "file_path": None,
    }
    await mw._on_compressed(request, event, started=0.0, forced=False)
    assert session.compression_count == 1


async def test_on_compressed_persists_audit_payload():
    records: list[tuple[dict, str]] = []

    async def audit(payload, summary):
        records.append((payload, summary))

    mw, session, model = make_middleware(audit=audit)
    mw._before_tokens = 5000
    msgs = make_messages(10)
    request = SimpleNamespace(messages=msgs, system_message=None, tools=[])
    event = {
        "cutoff_index": 8,
        "summary_message": HumanMessage(content="摘要"),
        "file_path": "/conversation_history/s.md",
    }
    await mw._on_compressed(request, event, started=0.0, forced=True)

    assert records
    payload, summary = records[0]
    assert payload["reason"] == "manual"
    assert payload["before_tokens"] == 5000
    assert payload["history_path"] == "/conversation_history/s.md"
    assert summary == "摘要"
    assert session.compression_count == 1
    assert session.last_compression_tokens == payload["after_tokens"]
    assert any(e["type"] == "context_compression" for e in session.hub._buffer)


def test_estimate_helpers_include_system_and_tools():
    mw, _, model = make_middleware()
    msgs = make_messages(3, size=10)
    request = SimpleNamespace(
        messages=msgs,
        system_message=HumanMessage(content="system"),
        tools=[],
    )
    assert estimate_request_tokens(request, mw._policy) > 0
    event = {
        "cutoff_index": 1,
        "summary_message": HumanMessage(content="S"),
        "file_path": None,
    }
    assert estimate_after_tokens(request, event, mw._policy) > 0


# --------------------------------------------------------------------------- 审计存储


@pytest.fixture()
def mongo_client():
    client = AsyncMongoMockClient()
    yield client
    client.close()


@pytest.fixture()
def store(mongo_client, monkeypatch):
    from backend.services.agent.session.store import AgentStore

    monkeypatch.setattr(
        "backend.services.agent.session.store.AsyncIOMotorClient",
        lambda uri, **kw: mongo_client,
    )
    return AgentStore("mongodb://fake", "crawler")


async def test_store_compaction_add_list_delete(store):
    await store.create_session("s1", "c", "T")
    await store.add_compaction(
        "s1", "c", summary="S", version=1, before_tokens=100, after_tokens=20
    )
    await store.add_compaction(
        "s1", "c", summary="S2", version=2, before_tokens=120, after_tokens=30
    )
    rows = await store.list_compactions("s1", "c")
    assert [r["version"] for r in rows] == [2, 1]
    assert rows[0]["summary"] == "S2"
    await store.delete_session("s1", "c")
    assert await store.list_compactions("s1", "c") == []
