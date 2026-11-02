"""Agent 上下文压缩: 阈值判定、结构化摘要、事件与审计。

复用 ``deepagents`` 的 ``SummarizationMiddleware`` 作为压缩引擎(offload、工具配对、
ContextOverflowError 兜底、二次压缩下标都由它处理), 本模块只补齐:

- 项目级阈值策略(上下文窗口 / 触发比例 / 保留窗口 / 防抖);
- 中文结构化摘要 prompt 与独立摘要模型;
- 实时事件(``context_compression_start`` / ``context_compression`` /
  ``context_compression_error``);
- 手动压缩的一次性强制开关;
- Mongo 审计回调(由 runner 注入)。

设计说明见 ``docs/agent-context-compression.md``。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Awaitable, Callable, Sequence

from deepagents.middleware.summarization import (
    SUMMARIZATION_EVENT_KEY,
    SummarizationMiddleware,
)
from langchain.agents.middleware.types import ModelRequest
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import AnyMessage, HumanMessage, get_buffer_string
from langchain_core.messages.utils import count_tokens_approximately

from ....config import Config
from ..prompts import COMPRESSION_PROMPT
from ..session.model import AgentSession
from .llm import build_chat_model
from .text import content_text

logger = logging.getLogger(__name__)

# 摘要里需要保留的产物路径特征
_PATH_RE = re.compile(r"/(?:saved|agent_saved|agent_backend)/[^\s\"'<>]+")

# 兜底摘要最多保留的最近文本条数
_FALLBACK_RECENT_TEXTS = 8


@dataclass
class CompressionPolicy:
    """压缩策略(全部来自 Config, 便于测试直接构造)。"""

    context_window: int = 128_000
    trigger_fraction: float = 0.62
    hard_fraction: float = 0.85
    keep_messages: int = 12
    trigger_messages: int = 90
    min_history_tokens: int = 8_000
    min_gap_messages: int = 6
    min_gap_tokens: int = 6_000
    summary_max_tokens: int = 1_200
    trim_tokens: int = 60_000
    timeout: float = 90.0
    chars_per_token: float = 1.8
    mode: str = "auto"  # auto / observe / off

    @classmethod
    def from_config(cls, cfg: Config) -> CompressionPolicy:
        """从全局配置构造策略; 关闭开关时等价于 off。

        使用 getattr 读取字段, 兼容测试/嵌入方传入的最小配置对象。
        """

        def value(name: str, default: Any) -> Any:
            raw = getattr(cfg, name, default)
            return default if raw is None else raw

        mode = str(value("compress_mode", "auto")).strip().lower()
        if not bool(value("context_compress", True)):
            mode = "off"
        if mode not in ("auto", "observe", "off"):
            mode = "auto"
        return cls(
            context_window=max(1024, int(value("context_window", 128_000))),
            trigger_fraction=float(value("compress_trigger_fraction", 0.62)),
            hard_fraction=float(value("compress_hard_fraction", 0.85)),
            keep_messages=max(2, int(value("compress_keep_messages", 12))),
            trigger_messages=max(4, int(value("compress_trigger_messages", 90))),
            min_history_tokens=max(0, int(value("compress_min_history_tokens", 8000))),
            min_gap_messages=max(0, int(value("compress_min_gap_messages", 6))),
            min_gap_tokens=max(0, int(value("compress_min_gap_tokens", 6000))),
            summary_max_tokens=max(200, int(value("compress_summary_max_tokens", 1200))),
            trim_tokens=max(1000, int(value("compress_trim_tokens", 60_000))),
            timeout=max(1.0, float(value("compress_timeout", 90.0))),
            chars_per_token=max(0.5, float(value("compress_chars_per_token", 1.8))),
            mode=mode,
        )

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def trigger_tokens(self) -> int:
        usable = self.context_window - 8192 - 4096
        return max(1024, min(int(self.context_window * self.trigger_fraction), usable))

    @property
    def hard_tokens(self) -> int:
        return max(1024, int(self.context_window * self.hard_fraction))

    def token_counter(self):
        """中文/截图友好的近似 token 计数器(可直接传给 deepagents)。"""
        return partial(
            count_tokens_approximately,
            chars_per_token=self.chars_per_token,
            tokens_per_image=900,
            use_usage_metadata_scaling=True,
        )

    def estimate(self, messages: Sequence[AnyMessage], tools: Any = None) -> int:
        counter = self.token_counter()
        try:
            return int(counter(messages, tools=tools or []))
        except TypeError:  # pragma: no cover - 兼容不支持 tools 参数的旧计数器
            return int(counter(messages))


def estimate_request_tokens(
    request: ModelRequest, policy: CompressionPolicy
) -> int:
    """估算一次模型请求: system prompt + tool schema + 消息。"""
    messages = list(request.messages)
    if request.system_message is not None:
        messages = [request.system_message, *messages]
    return policy.estimate(messages, tools=request.tools)


def estimate_after_tokens(
    request: ModelRequest, event: dict[str, Any], policy: CompressionPolicy
) -> int:
    """估算压缩后模型实际看到的上下文(摘要 + 保留窗口)。"""
    cutoff = int(event.get("cutoff_index") or 0)
    messages = [event.get("summary_message"), *request.messages[cutoff:]]
    messages = [m for m in messages if m is not None]
    if request.system_message is not None:
        messages = [request.system_message, *messages]
    return policy.estimate(messages, tools=request.tools)


def _cap_summary(text: str, max_tokens: int, chars_per_token: float) -> str:
    """按 token 预算截断摘要, 尽量停在换行处。"""
    text = (text or "").strip()
    if not text:
        return ""
    approx = count_tokens_approximately(
        [HumanMessage(content=text)], chars_per_token=chars_per_token
    )
    if approx <= max_tokens:
        return text
    limit = max(200, int(max_tokens * chars_per_token))
    cut = text[:limit]
    newline = cut.rfind("\n")
    if newline > limit // 2:
        cut = cut[:newline]
    return cut.rstrip() + "\n\n...[摘要已按预算截断]"


def _shrink_messages(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """摘要请求超窗时, 去掉中段、保留头尾。"""
    items = list(messages)
    if len(items) <= 4:
        return items
    head = items[: max(1, len(items) // 4)]
    tail = items[-max(1, len(items) // 3) :]
    return [*head, HumanMessage(content="[...中间历史已省略...]"), *tail]


def _fallback_summary(messages: Sequence[AnyMessage]) -> str:
    """摘要模型不可用时的确定性兜底: 目标 + 最近进展 + 产物路径。"""
    goal = ""
    recent: list[str] = []
    paths: list[str] = []
    for msg in messages:
        text = content_text(getattr(msg, "content", "")).strip()
        if not text:
            continue
        if not goal and isinstance(msg, HumanMessage):
            goal = text[:500]
        recent.append(text[:400])
        paths.extend(_PATH_RE.findall(text))
    lines = ["## 任务目标", goal or "(未记录)", "", "## 最近进展"]
    if recent:
        for text in recent[-_FALLBACK_RECENT_TEXTS:]:
            lines.append(f"- {text}")
    else:
        lines.append("- (无可用历史)")
    lines.extend(["", "## 产物与文件"])
    uniq = list(dict.fromkeys(paths))[:20]
    if uniq:
        lines.extend(f"- {p}" for p in uniq)
    else:
        lines.append("- (未记录)")
    lines.extend(
        [
            "",
            "## 未解决问题与下一步",
            "- 摘要模型暂不可用, 已使用确定性兜底; 需要细节时请读取 offload 历史文件。",
        ]
    )
    return "\n".join(lines)


class SessionCompressionMiddleware(SummarizationMiddleware):
    """在 deepagents 压缩器上补充项目阈值、中文摘要、事件、审计与手动触发。

    ``name`` 必须与 deepagents 内置实现同名, ``create_deep_agent`` 才会原位替换
    默认压缩器, 而不是追加第二个压缩器。
    """

    @property
    def name(self) -> str:
        return "SummarizationMiddleware"

    def __init__(
        self,
        cfg: Config,
        session: AgentSession,
        model: Any,
        backend: Any,
        policy: CompressionPolicy | None = None,
        summary_model: Any = None,
        audit: Callable[[dict[str, Any], str], Awaitable[None]] | None = None,
    ) -> None:
        self._cfg = cfg
        self._session = session
        self._policy = policy or CompressionPolicy.from_config(cfg)
        self._main_model = model
        self._summary_model_override = summary_model
        self._audit = audit
        self._force_next = False
        self._before_tokens = 0
        self._last_observe_count = 0

        trigger: list[tuple[str, int]]
        if self._policy.enabled:
            trigger = [
                ("tokens", self._policy.trigger_tokens),
                ("messages", self._policy.trigger_messages),
            ]
        else:
            # 关闭时仍保留中间件: 已有 _summarization_event 需要继续生效,
            # 但把阈值调到不可达, 不再产生新的压缩。
            trigger = [("tokens", 10**9)]

        super().__init__(
            model=model,
            backend=backend,
            trigger=trigger,
            keep=("messages", self._policy.keep_messages),
            token_counter=self._policy.token_counter(),
            trim_tokens_to_summarize=self._policy.trim_tokens,
            truncate_args_settings={
                "trigger": ("messages", max(20, self._policy.keep_messages + 8)),
                "keep": ("messages", self._policy.keep_messages),
                "max_length": 2000,
                "truncation_text": "\n...[旧工具参数已截断, 完整内容见对应工具结果/文件]",
            },
            summarizer=self._summarize,
        )

    # ------------------------------------------------------------ 判定

    def _should_summarize(self, messages: list[AnyMessage], total_tokens: int) -> bool:
        """在 deepagents 阈值之上叠加: 手动强制、历史下限、防抖与 observe。"""
        if self._policy.mode == "off":
            return False

        if self._force_next:
            # 无论能否压缩都消费一次性标记, 避免每次模型调用重复报错
            self._force_next = False
            return self._can_compress(messages, total_tokens, manual=True)

        triggered = super()._should_summarize(messages, total_tokens)
        if not triggered:
            return False

        if self._policy.mode == "observe":
            self._emit_observed(messages, total_tokens)
            return False

        if not self._can_compress(messages, total_tokens):
            return False
        if total_tokens >= self._policy.hard_tokens:
            return True
        return self._gap_ok(messages, total_tokens)

    def _can_compress(
        self, messages: Sequence[AnyMessage], total_tokens: int, manual: bool = False
    ) -> bool:
        keep = self._policy.keep_messages
        if len(messages) <= keep + 2:
            return False
        tail = list(messages[-keep:]) if keep > 0 else []
        old_tokens = max(0, total_tokens - self._policy.estimate(tail))
        floor = self._policy.min_history_tokens // 2 if manual else self._policy.min_history_tokens
        return old_tokens >= floor

    def _gap_ok(self, messages: Sequence[AnyMessage], total_tokens: int) -> bool:
        last_count = int(getattr(self._session, "last_compression_message_count", 0) or 0)
        last_tokens = int(getattr(self._session, "last_compression_tokens", 0) or 0)
        if last_count <= 0:
            return True
        if len(messages) - last_count < self._policy.min_gap_messages:
            return False
        if last_tokens and total_tokens - last_tokens < self._policy.min_gap_tokens:
            return False
        return True

    def _emit_observed(self, messages: list[AnyMessage], total_tokens: int) -> None:
        """observe 模式: 只上报“将压缩/可释放 token”, 不修改上下文。"""
        if len(messages) - self._last_observe_count < self._policy.min_gap_messages:
            return
        try:
            cutoff = self._determine_cutoff_index(messages)
        except Exception:  # noqa: BLE001
            return
        if cutoff <= 0:
            return
        self._last_observe_count = len(messages)
        after = total_tokens - self._policy.estimate(messages[:cutoff])
        self._session.emit(
            {
                "type": "context_compression",
                "applied": False,
                "reason": "observe",
                "before_tokens": total_tokens,
                "after_tokens": max(0, after),
                "saved_tokens": max(0, total_tokens - after),
                "message_count_before": len(messages),
                "message_count_after": len(messages) - cutoff + 1,
                "summarized_messages": max(0, cutoff - 1),
                "keep_messages": self._policy.keep_messages,
                "version": int(getattr(self._session, "compression_count", 0) or 0),
            }
        )

    # ------------------------------------------------------------ 模型调用钩子

    def _effective_messages(self, request: ModelRequest) -> list[AnyMessage]:
        """应用上一次压缩事件后, 模型实际会看到的 messages。"""
        getter = getattr(self, "_get_effective_messages", None)
        if getter is None:
            return list(request.messages)
        try:
            return list(getter(request))
        except Exception:  # noqa: BLE001
            return list(request.messages)

    async def awrap_model_call(self, request: ModelRequest, handler):
        before_messages = self._effective_messages(request)
        if request.system_message is not None:
            before_messages = [request.system_message, *before_messages]
        self._before_tokens = self._policy.estimate(
            before_messages, tools=request.tools
        )
        forced = self._begin_forced_call()
        started = time.monotonic()
        response = await super().awrap_model_call(request, handler)
        update = getattr(getattr(response, "command", None), "update", None) or {}
        event = update.get(SUMMARIZATION_EVENT_KEY)
        if event:
            await self._on_compressed(request, event, started, forced)
        elif forced:
            # 手动请求但可压缩历史不足 / cutoff <= 0: 明确告知, 不静默丢失
            self._session.emit(
                {
                    "type": "context_compression_error",
                    "reason": "manual",
                    "error": "可压缩历史不足, 未执行压缩",
                }
            )
        return response

    def _begin_forced_call(self) -> bool:
        requested = bool(getattr(self._session, "compress_requested", False))
        if not requested or not self._policy.enabled:
            return False
        self._session.compress_requested = False
        self._force_next = True
        return True

    # ------------------------------------------------------------ 摘要

    def _resolve_summary_model(self):
        if self._summary_model_override is not None:
            return self._summary_model_override
        compress_model = getattr(self._cfg, "compress_model", "") or ""
        if compress_model:
            return build_chat_model(
                replace(self._cfg, llm_model=compress_model)
            )
        return self._main_model

    async def _summarize(self, messages: Sequence[AnyMessage]) -> str:
        items = list(messages)
        self._session.emit(
            {
                "type": "context_compression_start",
                "before_tokens": self._before_tokens,
                "summarized_messages": len(items),
            }
        )
        degraded = False
        try:
            summary = await self._call_summary_model(items)
        except ContextOverflowError:
            # 摘要请求本身超窗: 去掉中段再试一次
            try:
                summary = await self._call_summary_model(_shrink_messages(items))
            except Exception as exc:  # noqa: BLE001
                degraded = True
                logger.warning("[ContextCompression] 摘要超窗后仍失败: %s", exc)
                summary = _fallback_summary(items)
        except Exception as exc:  # noqa: BLE001
            degraded = True
            logger.warning("[ContextCompression] 摘要失败, 使用兜底摘要: %s", exc)
            summary = _fallback_summary(items)

        summary = _cap_summary(
            summary, self._policy.summary_max_tokens, self._policy.chars_per_token
        )
        if degraded or not summary:
            self._session.emit(
                {
                    "type": "context_compression_error",
                    "reason": "auto",
                    "error": "摘要模型调用失败, 已使用确定性兜底摘要",
                }
            )
        return summary

    async def _call_summary_model(self, messages: Sequence[AnyMessage]) -> str:
        prompt = COMPRESSION_PROMPT.format(
            messages=get_buffer_string(list(messages), format="xml")
        )
        model = self._resolve_summary_model()
        resp = await asyncio.wait_for(
            model.ainvoke(prompt), timeout=self._policy.timeout
        )
        return _cap_summary(
            content_text(getattr(resp, "content", "")),
            self._policy.summary_max_tokens,
            self._policy.chars_per_token,
        )

    # ------------------------------------------------------------ 压缩完成

    async def _on_compressed(
        self,
        request: ModelRequest,
        event: dict[str, Any],
        started: float,
        forced: bool,
    ) -> None:
        before = self._before_tokens
        after = estimate_after_tokens(request, event, self._policy)
        count_before = len(request.messages)
        cutoff = int(event.get("cutoff_index") or 0)
        count_after = max(1, count_before - cutoff + 1)
        version = int(getattr(self._session, "compression_count", 0) or 0) + 1
        self._session.compression_count = version
        self._session.last_compression_message_count = count_after
        self._session.last_compression_tokens = after
        summary_text = content_text(
            getattr(event.get("summary_message"), "content", "")
        )
        payload: dict[str, Any] = {
            "type": "context_compression",
            "applied": True,
            "reason": "manual" if forced else "auto",
            "before_tokens": before,
            "after_tokens": after,
            "saved_tokens": max(0, before - after),
            "message_count_before": count_before,
            "message_count_after": count_after,
            "summarized_messages": max(0, count_before - count_after),
            "keep_messages": self._policy.keep_messages,
            "summary_chars": len(summary_text),
            "history_path": event.get("file_path"),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "version": version,
            "model": getattr(self._cfg, "compress_model", "")
            or getattr(self._cfg, "llm_model", ""),
        }
        self._session.last_compression = payload
        self._session.emit(payload)
        if self._audit is not None:
            try:
                await self._audit(payload, summary_text)
            except Exception as exc:  # noqa: BLE001  审计失败不影响任务
                logger.warning("[ContextCompression] 审计写入失败: %s", exc)


def build_compression_middleware(
    cfg: Config,
    session: AgentSession,
    model: Any,
    backend: Any,
    audit: Callable[[dict[str, Any], str], Awaitable[None]] | None = None,
    summary_model: Any = None,
) -> SessionCompressionMiddleware:
    """构建会话压缩中间件(同名替换 deepagents 默认压缩器)。"""
    return SessionCompressionMiddleware(
        cfg=cfg,
        session=session,
        model=model,
        backend=backend,
        policy=CompressionPolicy.from_config(cfg),
        summary_model=summary_model,
        audit=audit,
    )
