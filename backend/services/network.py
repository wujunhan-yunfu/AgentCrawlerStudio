"""网络频道: 通过 CDP Network 域记录请求并扇出到 /ws/network。

requestWillBeSent / responseReceived / loadingFinished / loadingFailed
→ 合并成一条 request 记录, 事件按 op 增量推送给前端, 前端按 id 合并。
"""

from __future__ import annotations

import time
from typing import Any

from .cdp import CDPManager, CDPSession


class NetworkChannel:
    name = "network"
    domains: list[tuple[str, dict[str, Any]]] = [
        ("Network.enable", {"maxPostDataSize": 65536, "maxTotalBufferSize": 16 * 1024 * 1024}),
    ]

    def __init__(self, mgr: CDPManager):
        self.mgr = mgr
        self.channel = mgr.register_channel(self)
        self._records: dict[str, dict[str, Any]] = {}
        self._session_of: dict[str, str] = {}
        # 请求开始时刻的 CDP 单调时间戳(与 finished 同刻度, 用于计算耗时)
        self._start_ts: dict[str, float] = {}

    # ------------------------------------------------------------ 事件

    async def on_event(self, session: CDPSession, method: str, params: dict[str, Any]) -> bool:
        if method == "Network.requestWillBeSent":
            await self._request_will_be_sent(session, params)
        elif method == "Network.responseReceived":
            await self._response_received(params)
        elif method == "Network.loadingFinished":
            await self._loading_finished(params)
        elif method == "Network.loadingFailed":
            await self._loading_failed(params)
        elif method == "Page.frameNavigated":
            # 主 frame 导航 → 前端据此决定是否清空记录(未勾选"保留日志")
            frame = params.get("frame") or {}
            if not frame.get("parentId"):
                await self.channel.publish({"type": "network", "op": "nav"})
            else:
                return False
        else:
            return False
        return True

    async def _request_will_be_sent(self, session: CDPSession, params: dict[str, Any]) -> None:
        rid = params.get("requestId", "")
        request = params.get("request") or {}
        url = request.get("url", "")
        self._session_of[rid] = session.ws_url
        self._start_ts[rid] = params.get("timestamp") or time.time()
        record = {
            "id": rid,
            "url": url,
            "method": request.get("method", ""),
            "status": None,
            "statusText": None,
            "mimeType": None,
            "type": params.get("type", "Other"),
            "started": params.get("wallTime") or time.time(),
            "finished": None,
            "duration": None,
            "size": None,
            "error": None,
            "canceled": False,
            "initiator": self._initiator_text(params.get("initiator")),
            "requestHeaders": request.get("headers") or {},
            "responseHeaders": None,
            "postData": self._post_data(request),
        }
        self._records[rid] = record
        await self.channel.publish({"type": "network", "op": "request", "record": record})

    async def _response_received(self, params: dict[str, Any]) -> None:
        rid = params.get("requestId", "")
        record = self._records.get(rid)
        if record is None:
            return
        response = params.get("response") or {}
        record["status"] = response.get("status")
        record["statusText"] = response.get("statusText")
        record["mimeType"] = response.get("mimeType")
        record["responseHeaders"] = response.get("headers") or {}
        await self.channel.publish({"type": "network", "op": "response", "record": record})

    async def _loading_finished(self, params: dict[str, Any]) -> None:
        rid = params.get("requestId", "")
        record = self._records.get(rid)
        if record is None:
            return
        record["finished"] = params.get("timestamp") or time.time()
        record["size"] = params.get("encodedDataLength")
        if record["started"]:
            start_ts = self._start_ts.get(rid, record["started"])
            record["duration"] = max(0, round((record["finished"] - start_ts) * 1000, 1))
        await self.channel.publish({"type": "network", "op": "finished", "record": record})

    async def _loading_failed(self, params: dict[str, Any]) -> None:
        rid = params.get("requestId", "")
        record = self._records.get(rid)
        if record is None:
            return
        record["error"] = params.get("errorText")
        record["canceled"] = bool(params.get("canceled"))
        record["finished"] = params.get("timestamp") or time.time()
        if record["started"]:
            start_ts = self._start_ts.get(rid, record["started"])
            record["duration"] = max(0, round((record["finished"] - start_ts) * 1000, 1))
        await self.channel.publish({"type": "network", "op": "failed", "record": record})

    @staticmethod
    def _initiator_text(initiator: dict[str, Any] | None) -> str | None:
        if not initiator:
            return None
        init_type = initiator.get("type")
        if init_type == "script":
            stack = initiator.get("stack") or {}
            frames = stack.get("callFrames") or []
            if frames:
                f = frames[0]
                return f"{f.get('url') or '<inline>'}:{(f.get('lineNumber') or 0) + 1}"
        return init_type or None

    @staticmethod
    def _post_data(request: dict[str, Any]) -> str | None:
        data = request.get("postData")
        if data is None:
            return None
        return data

    # ------------------------------------------------------------ 命令接口

    async def clear(self) -> dict[str, Any]:
        self._records.clear()
        self._session_of.clear()
        self._start_ts.clear()
        self.channel.clear_history()
        await self.channel.publish({"type": "network", "op": "clear"})
        return {"ok": True}

    async def body(self, request_id: str) -> dict[str, Any]:
        session = self.mgr.session_for(self._session_of.get(request_id))
        if session is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await session.command("Network.getResponseBody", {"requestId": request_id}, timeout=5.0)
            # 记录的会话缓冲可能已随会话重建失效, 回退到当前活动会话再试一次
            if "error" in resp:
                primary = self.mgr.primary()
                if primary is not None and primary.ws_url != session.ws_url:
                    resp = await primary.command("Network.getResponseBody", {"requestId": request_id}, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "无法获取响应体"))}
        result = resp.get("result") or {}
        return {
            "ok": True,
            "body": result.get("body", ""),
            "base64_encoded": bool(result.get("base64Encoded")),
        }
