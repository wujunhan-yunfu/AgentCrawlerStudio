"""控制台频道: 通过 CDP 监听 DevTools Console 事件并扇出到 /ws/console。

处理 consoleAPICalled / exceptionThrown / Log.entryAdded, 与 DevTools 对齐:
格式符 %s/%d/%o/%c、对象展开(objectId)、console.group/count/table/assert/clear 等。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .cdp import CDPManager, CDPSession

# CDP consoleAPICalled.type -> 展示级别
_LEVEL_MAP = {
    "log": "log",
    "info": "info",
    "warning": "warning",
    "error": "error",
    "debug": "debug",
    "dir": "info",
    "dirxml": "info",
    "table": "info",
    "trace": "log",
    "startGroup": "log",
    "startGroupCollapsed": "log",
    "group": "log",
    "groupCollapsed": "log",
    "profile": "info",
    "profileEnd": "info",
    "timeEnd": "info",
    "timeLog": "info",
    "count": "log",
    "assert": "error",
}

_LOG_LEVEL_MAP = {
    "verbose": "debug",
    "info": "info",
    "warning": "warning",
    "error": "error",
}

_SPECIFIER_RE = re.compile(r"%(s|d|i|f|o|O|c|%)")


class ConsoleChannel:
    """浏览器控制台频道(事件处理 + 消息格式化)。"""

    name = "console"
    domains: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, mgr: CDPManager):
        self.mgr = mgr
        self.channel = mgr.register_channel(self)

    # ------------------------------------------------------------ 事件

    async def on_event(self, session: CDPSession, method: str, params: dict[str, Any]) -> bool:
        msg: dict[str, Any] | None = None
        if method == "Runtime.consoleAPICalled":
            msg = await self._console_api(session, params)
        elif method == "Runtime.exceptionThrown":
            msg = self._exception_thrown(params)
        elif method == "Log.entryAdded":
            msg = self._log_entry(params)
        else:
            return False
        if msg is not None:
            await self.channel.publish(msg)
        return True

    async def _console_api(self, session: CDPSession, params: dict[str, Any]) -> dict[str, Any] | None:
        ctype = params.get("type", "log")
        level = _LEVEL_MAP.get(ctype, "log")
        args = params.get("args") or []
        url = params.get("url")
        line = params.get("lineNumber")
        stack = self.mgr._stack_text(params.get("stackTrace"))
        ts = params.get("timestamp")
        kind = ctype
        extra: dict[str, Any] = {}

        # 过滤 Playwright 内部标记消息(set_content 等注入脚本的完成标记)
        if args and args[0].get("type") == "string":
            first = str(args[0].get("value") or "")
            if first.startswith("--playwright--"):
                return None

        if ctype == "clear":
            return self._make_message("clear", "log", [], ts=ts, group=session.group_depth)

        if ctype in ("startGroup", "startGroupCollapsed", "group", "groupCollapsed"):
            session.group_depth += 1
            segments = self._build_segments(session, args)
            return self._make_message(
                ctype, level, segments, url=url, line=line, stack=stack,
                ts=ts, group=session.group_depth - 1,
            )

        if ctype == "endGroup":
            session.group_depth = max(0, session.group_depth - 1)
            return self._make_message("groupEnd", "log", [], ts=ts, group=session.group_depth)

        if ctype == "assert":
            rest = args[1:] if args and args[0].get("type") == "boolean" else args
            segments = self._build_segments(session, rest)
            if not segments:
                segments = [self._text_segment("Assertion failed", "str")]
            return self._make_message("assert", "error", segments, url=url, line=line,
                                      stack=stack, ts=ts, group=session.group_depth)

        if ctype == "trace":
            segments = self._build_segments(session, args)
            return self._make_message("trace", level, segments, url=url, line=line,
                                      stack=stack, ts=ts, group=session.group_depth)

        if ctype == "table":
            segments = self._build_segments(session, args)
            first_arg = args[0] if args else {}
            oid = first_arg.get("objectId")
            if oid:
                rows = await self._stringify(session, oid)
                if rows is not None:
                    try:
                        extra["table"] = json.loads(rows)
                    except ValueError:
                        extra["table"] = None
            return self._make_message("table", level, segments, url=url, line=line,
                                      ts=ts, group=session.group_depth, **extra)

        # count / time / timeLog / timeEnd 及常规类型: Chrome 已在 args 中预格式化
        segments = self._build_segments(session, args)
        return self._make_message(kind, level, segments, url=url, line=line, stack=stack,
                                  ts=ts, group=session.group_depth)

    def _exception_thrown(self, params: dict[str, Any]) -> dict[str, Any]:
        details = params.get("exceptionDetails") or {}
        exc = details.get("exception") or {}
        description = exc.get("description") or exc.get("value")
        if description is None and exc.get("objectId"):
            description = f"<{exc.get('subtype') or exc.get('type')}>"
        if not description:
            description = details.get("text") or "Uncaught exception"
        return self._make_message(
            "exception", "error",
            [{"k": "obj", "v": str(description), "oid": exc.get("objectId"),
              "sub": exc.get("subtype"), "cls": exc.get("className")}],
            url=details.get("url"), line=details.get("lineNumber"),
            stack=self.mgr._stack_text(details.get("stackTrace")),
            ts=params.get("timestamp"),
        )

    def _log_entry(self, params: dict[str, Any]) -> dict[str, Any]:
        entry = params.get("entry") or {}
        level = _LOG_LEVEL_MAP.get(str(entry.get("level", "")), "log")
        source = entry.get("source") or ""
        text = entry.get("text") or ""
        prefix = f"[{source}] " if source else ""
        return self._make_message(
            "entry", level, [self._text_segment(prefix + text, "str")],
            url=entry.get("url"), line=entry.get("lineNumber"),
            ts=params.get("timestamp"),
        )

    # ------------------------------------------------------------ 消息构造

    def _make_message(self, kind: str, level: str, segments: list[dict[str, Any]],
                      *, url=None, line=None, stack=None, ts=None, group: int = 0,
                      table: Any = None, **extra) -> dict[str, Any]:
        line = line + 1 if isinstance(line, int) else None
        if isinstance(ts, (int, float)):
            ts_sec = float(ts)
            if ts_sec > 1e11:  # CDP 部分事件返回毫秒, 统一换算为秒
                ts_sec /= 1000.0
        else:
            ts_sec = time.time()
        msg: dict[str, Any] = {
            "type": "console",
            "kind": kind,
            "level": level,
            "text": self._segments_text(segments),
            "items": segments,
            "url": url,
            "line": line,
            "stack": stack,
            "ts": round(ts_sec, 3),
            "group": group,
        }
        if table is not None:
            msg["table"] = table
        msg.update(extra)
        return msg

    @staticmethod
    def _segments_text(segments: list[dict[str, Any]]) -> str:
        return "".join(str(s.get("v") or "") for s in segments)

    @staticmethod
    def _text_segment(value: str, t: str = "str") -> dict[str, Any]:
        return {"k": "text", "t": t, "v": value}

    # ------------------------------------------------------------ 参数渲染

    def _build_segments(self, session: CDPSession | None, args: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 consoleAPICalled 的 args 转为前端 segments(处理 %s/%d/%f/%o/%O/%c/%%)。"""
        if not args:
            return []

        def spaced(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for item in items:
                if out:
                    out.append(self._text_segment(" "))
                out.append(item)
            return out

        first = args[0]
        if first.get("type") != "string":
            return spaced([self.mgr.remote_item(session, a) for a in args])

        fmt = str(first.get("value") or "")
        if not _SPECIFIER_RE.search(fmt):
            return spaced([self.mgr.remote_item(session, a) for a in args])

        segments: list[dict[str, Any]] = []
        arg_idx = 1
        i = 0
        buf = ""
        pending_style: str | None = None
        while i < len(fmt):
            if fmt[i] == "%" and i + 1 < len(fmt):
                spec = fmt[i + 1]
                if spec == "%":
                    buf += "%"
                    i += 2
                    continue
                if spec in "sdfiocO":
                    if buf:
                        segments.append(self._text_segment(buf))
                        buf = ""
                    if spec == "c":
                        style_arg = args[arg_idx] if arg_idx < len(args) else {}
                        arg_idx += 1
                        pending_style = self.mgr._remote_str(style_arg)
                        i += 2
                        continue
                    arg = args[arg_idx] if arg_idx < len(args) else {}
                    arg_idx += 1
                    if spec in ("o", "O"):
                        segments.append(self.mgr.remote_item(session, arg))
                    else:
                        buf += self._spec_value(spec, arg)
                    i += 2
                    continue
            buf += fmt[i]
            i += 1
        if buf:
            segments.append(self._text_segment(buf))
        for arg in args[arg_idx:]:
            segments.append(self._text_segment(" "))
            segments.append(self.mgr.remote_item(session, arg))
        if pending_style:
            for seg in segments:
                if seg["k"] == "text" and seg.get("v"):
                    seg["style"] = pending_style
                    break
        return segments

    @staticmethod
    def _spec_value(spec: str, arg: dict[str, Any]) -> str:
        v = arg.get("value")
        try:
            if spec == "s":
                return CDPManager._remote_str(arg)
            if spec in ("d", "i"):
                return str(int(float(v or 0)))
            if spec == "f":
                return f"{float(v or 0):g}"
        except (TypeError, ValueError):
            return "NaN"
        return ""

    async def _stringify(self, session: CDPSession, object_id: str) -> str | None:
        """把对象 JSON 序列化为字符串(用于 console.table), 失败返回 None。"""
        try:
            resp = await session.command("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": "function(){ try { return JSON.stringify(this) } catch(e) { return undefined } }",
                "returnByValue": True,
                "silent": True,
            }, timeout=3.0)
        except Exception:  # noqa: BLE001
            return None
        result = resp.get("result") or {}
        if result.get("exceptionDetails"):
            return None
        value = (result.get("result") or {}).get("value")
        return value if isinstance(value, str) else None
