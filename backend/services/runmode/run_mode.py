"""run 模式执行器: 一次性(once)或定时(cron)运行 Mongo HEAD 代码(见规划第 7 章)。

- 每次运行发 run.started / run.succeeded / run.failed webhook(登录失败归入 failed);
- cron 常驻进程周期性心跳探活(heartbeat.py);
- save_content 数据经 data_webhook.py 逐条推送 CDC;
- 一次性运行返回退出码(0 成功 / 非 0 失败), 供 K8s Job/Pod 判定。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

from ...config import Config
from ..cron import next_runs
from .code_source import NoCodeError, load_head_code
from .data_webhook import DataCollector
from .heartbeat import HeartbeatSender
from .run_context import RunContext
from .runtime import _MongoTicketStore, execute, resolve_headless
from .webhook import WebhookSender

_EVENT_FOR_STATUS = {
    "running": "run.started",
    "succeeded": "run.succeeded",
    "failed": "run.failed",
    "skipped": "run.skipped",
}

_EXIT_CODE = {"no_code": 2, "cron_invalid": 2, "internal": 1}


def _derive_endpoint(base: str, suffix: str) -> str:
    """从运行 webhook 基址推导同源端点(/webhooks/runs -> /webhooks/heartbeat|data)。"""
    if not (base or "").strip():
        return ""
    stripped = base.strip().rstrip("/")
    if stripped.endswith("/runs"):
        stripped = stripped[: -len("/runs")]
    return f"{stripped}{suffix}"


def resolve_urls(cfg: Config) -> tuple[str, str, str]:
    """返回 (运行事件 webhook, 心跳, 数据 webhook) 地址。"""
    base = (cfg.webhook_url or "").strip()
    heartbeat = (cfg.heartbeat_url or "").strip() or _derive_endpoint(base, "/heartbeat")
    data = (cfg.data_webhook_url or "").strip() or _derive_endpoint(base, "/data")
    return base, heartbeat, data


def build_context(cfg: Config) -> RunContext:
    import os

    return RunContext(
        crawler_id=(cfg.crawler_id or "default"),
        run_id=cfg.run_id or "",
        mode="run",
        run_type=(cfg.run_type or "once"),
        cron=(cfg.cron or "").strip(),
        project_id=os.environ.get("PROJECT_ID", ""),
        workspace_id=os.environ.get("WORKSPACE_ID", ""),
    )


def classify_error(error: str) -> str:
    """把运行错误归类为 webhook error.type(登录失败/验证失败/代码错误等)。"""
    low = (error or "").lower()
    if "verificationfailed" in low or "验证码" in low or "验证" in low:
        return "verification_failed"
    if "cancelled" in low or "登录" in low or "login" in low:
        return "login_failed"
    return "code_error"


def _saved_summary(items: list[dict[str, Any]], collector: DataCollector | None) -> list[dict]:
    out: list[dict[str, Any]] = []
    for it in items or []:
        save_id = ""
        delivery = "local"
        remote_path = ""
        if collector is not None and collector.enabled:
            save_id = collector.save_id_for(it)
            delivery = collector.delivery_for(save_id)
            remote_path = ""
        out.append(
            {
                "name": it.get("name", ""),
                "kind": it.get("kind", "content"),
                "size": it.get("size", 0),
                "save_id": save_id,
                "delivery": delivery,
                "remote_path": remote_path,
            }
        )
    return out


async def _notify(sender: WebhookSender, ctx: RunContext) -> None:
    event = _EVENT_FOR_STATUS.get(ctx.status, f"run.{ctx.status}")
    payload = {"event": event, "run": ctx.to_dict()}
    try:
        await sender.send_with_retry(event, payload)
    except Exception as exc:  # noqa: BLE001
        print(f"[webhook] {event} 发送失败: {exc}", file=sys.stderr)


async def _run_once(
    cfg: Config,
    ctx: RunContext,
    sender: WebhookSender,
    collector: DataCollector,
    tickets: _MongoTicketStore,
    heartbeat: HeartbeatSender | None,
) -> int:
    """执行一次爬取并发送开始/结束 webhook, 返回退出码。"""
    ctx.set_status("running")
    ctx.error_type = ctx.error_message = ctx.error_detail = ""
    ctx.saved = []
    if heartbeat is not None:
        heartbeat.ping("running")
    await _notify(sender, ctx)

    try:
        code = await load_head_code(cfg)
    except NoCodeError as exc:
        ctx.set_status("failed", error_type="no_code", error_message=str(exc))
        await _notify(sender, ctx)
        return _EXIT_CODE["no_code"]

    async def on_saved(item: dict[str, Any]) -> None:
        await collector.collect(item)

    result = await execute(
        cfg,
        code,
        on_saved=on_saved,
        tickets=tickets,
        headless=resolve_headless(cfg),
    )
    await collector.flush()

    if result.get("ok"):
        ctx.set_status("succeeded")
    else:
        error = result.get("error") or "未知错误"
        ctx.set_status(
            "failed",
            error_type=classify_error(error),
            error_message=str(error)[:500],
            error_detail=str(error),
        )
    ctx.saved = _saved_summary(result.get("saved") or [], collector)
    if heartbeat is not None:
        heartbeat.ping("alive")
    await _notify(sender, ctx)
    return 0 if ctx.status == "succeeded" else 1


async def _cron_loop(
    cfg: Config,
    ctx: RunContext,
    sender: WebhookSender,
    collector: DataCollector,
    tickets: _MongoTicketStore,
    heartbeat: HeartbeatSender | None,
) -> int:
    expr = (cfg.cron or "").strip()
    try:
        next_runs(expr, count=1)
    except Exception as exc:  # noqa: BLE001
        print(f"cron 表达式无效: {exc}", file=sys.stderr)
        return _EXIT_CODE["cron_invalid"]
    while True:
        now = datetime.now(timezone.utc)
        try:
            upcoming = next_runs(expr, count=1, base=now)[0]
        except Exception as exc:  # noqa: BLE001
            print(f"计算下次运行时间失败: {exc}", file=sys.stderr)
            return _EXIT_CODE["internal"]
        wait = max(0.0, (upcoming - now).total_seconds())
        ctx.next_run_at = int(upcoming.timestamp() * 1000)
        print(
            f"下次运行: {upcoming.isoformat()} (等待 {wait:.0f}s)",
            flush=True,
        )
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return 0
        ctx.next_run_at = 0
        exit_code = await _run_once(cfg, ctx, sender, collector, tickets, heartbeat)
        ctx.last_run = {
            "run_id": ctx.run_id,
            "status": ctx.status,
            "started_at": ctx.started_at,
            "finished_at": ctx.finished_at,
        }


async def _main_async(
    cfg: Config,
    ctx: RunContext,
    sender: WebhookSender,
    collector: DataCollector,
    heartbeat: HeartbeatSender | None,
    tickets: _MongoTicketStore,
) -> int:
    collector.start()
    if heartbeat is not None:
        heartbeat.start()
    try:
        if (cfg.run_type or "once") == "cron":
            return await _cron_loop(cfg, ctx, sender, collector, tickets, heartbeat)
        return await _run_once(cfg, ctx, sender, collector, tickets, heartbeat)
    finally:
        if heartbeat is not None:
            await heartbeat.stop()
        await collector.stop()


def run_mode_main(cfg: Config) -> int:
    """run 模式入口(同步): 阻塞直到完成, 返回进程退出码。"""
    base, heartbeat_url, data_url = resolve_urls(cfg)
    ctx = build_context(cfg)
    sender = WebhookSender(base, cfg.webhook_secret)
    collector = DataCollector(
        cfg,
        ctx,
        WebhookSender(data_url, cfg.webhook_secret),
        sync=bool(cfg.data_webhook_sync),
        inline_max_bytes=cfg.data_inline_max_bytes,
    )
    heartbeat = None
    if (cfg.run_type or "once") == "cron":
        heartbeat = HeartbeatSender(
            cfg, ctx, WebhookSender(heartbeat_url, cfg.webhook_secret),
            interval=cfg.heartbeat_interval,
        )
    tickets = _MongoTicketStore(cfg)
    try:
        return asyncio.run(
            _main_async(cfg, ctx, sender, collector, heartbeat, tickets)
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"run 模式执行失败: {exc}", file=sys.stderr)
        return _EXIT_CODE["internal"]