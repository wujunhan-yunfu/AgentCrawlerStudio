"""webhook 发送器: HMAC 签名 + 指数退避重试。

运行事件(7.7/7.9)、心跳(7.11)、数据推送(7.12)共用一套签名协议:
- X-Webhook-Event       事件名
- X-Webhook-Delivery    投递唯一 ID(uuid), 供接收方幂等
- X-Webhook-Timestamp   毫秒时间戳
- X-Webhook-Signature   sha256=HMAC_SHA256(secret, timestamp + "." + body)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx

_BACKOFFS = (1.0, 5.0, 30.0, 60.0, 120.0)
_DEFAULT_ATTEMPTS = 5
_DEFAULT_TIMEOUT = 15.0


class WebhookError(Exception):
    """webhook 投递失败(重试耗尽或不可恢复错误)。"""


def sign(secret: str, timestamp: str, body: str) -> str:
    """HMAC-SHA256 签名: sha256=hexdigest(secret, timestamp + '.' + body)。"""
    payload = f"{timestamp}.{body}".encode("utf-8")
    digest = hmac.new((secret or "").encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_headers(
    event: str,
    delivery_id: str,
    timestamp: str,
    body: str,
    secret: str = "",
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Webhook-Event": event,
        "X-Webhook-Delivery": delivery_id,
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": sign(secret, timestamp, body),
    }


class WebhookSender:
    """向 CDC 发送 webhook。URL 为空时所有发送为 no-op。"""

    def __init__(
        self,
        url: str = "",
        secret: str = "",
        max_attempts: int = _DEFAULT_ATTEMPTS,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.url = (url or "").strip()
        self.secret = secret or ""
        self.max_attempts = max(1, max_attempts)
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def send(
        self,
        event: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """发送一次并返回 CDC 响应 JSON; URL 为空或不可用时返回 None。"""
        if not self.enabled:
            return None
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        delivery_id = uuid.uuid4().hex
        headers = build_headers(event, delivery_id, timestamp, body, self.secret)
        if extra_headers:
            headers.update(extra_headers)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, content=body, headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise WebhookError(f"webhook {event} 请求失败: {exc}") from exc
        text = resp.text or ""
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"raw": text}
        if resp.status_code >= 400:
            raise WebhookError(
                f"webhook {event} -> {self.url} 失败: HTTP {resp.status_code}: {text[:200]}"
            )
        return data

    async def send_with_retry(self, event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """带指数退避重试的投递, 重试耗尽抛 WebhookError。"""
        if not self.enabled:
            return None
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            if attempt > 0:
                await asyncio.sleep(_BACKOFFS[min(attempt - 1, len(_BACKOFFS) - 1)])
            try:
                return await self.send(event, payload)
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise WebhookError(
            f"webhook {event} 重试 {self.max_attempts} 次后仍失败: {last}"
        ) from last