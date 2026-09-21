"""运行上下文: 记录单次运行(login/run)的元信息、状态与产物摘要。

供 webhook 报文、心跳报文、运行结束判定与退出码共用。
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class LoginComplete(Exception):
    """login 模式: 凭据保存成功后由编排层抛出, 结束本次登录运行(非错误)。"""


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class RunContext:
    """单次运行的元信息与状态。"""

    crawler_id: str = "default"
    run_id: str = ""
    project_id: str = ""
    workspace_id: str = ""
    mode: str = "run"                 # run / login
    run_type: str = "once"            # once / cron
    cron: str = ""
    process_id: str = ""
    process_status: str = "alive"     # alive / running / degraded / stopping(心跳用)
    status: str = "pending"           # pending / running / succeeded / failed / skipped
    started_at: int = 0
    finished_at: int = 0
    duration_ms: int = 0
    error_type: str = ""
    error_message: str = ""
    error_detail: str = ""
    saved: list[dict[str, Any]] = field(default_factory=list)
    next_run_at: int = 0
    last_run: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = new_run_id()
        if not self.process_id:
            self.process_id = new_run_id()
        if not self.started_at:
            self.started_at = now_ms()

    def set_status(
        self,
        status: str,
        error_type: str = "",
        error_message: str = "",
        error_detail: str = "",
    ) -> None:
        self.status = status
        self.error_type = error_type
        self.error_message = error_message
        self.error_detail = error_detail
        if status in ("succeeded", "failed", "skipped"):
            self.finished_at = now_ms()
            self.duration_ms = self.finished_at - self.started_at

    def bump_seq(self) -> int:
        self.seq += 1
        return self.seq

    def to_dict(self) -> dict[str, Any]:
        """webhook run 报文(见规划 7.7)。"""
        return {
            "run_id": self.run_id,
            "crawler_id": self.crawler_id,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "mode": self.mode,
            "trigger": self.run_type,
            "run_type": self.run_type,
            "cron": self.cron,
            "process_id": self.process_id,
            "attempt": 1,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "error": (
                {
                    "type": self.error_type,
                    "message": self.error_message,
                    "detail": self.error_detail,
                }
                if self.error_type
                else None
            ),
            "saved": self.saved,
            "next_run_at": self.next_run_at,
        }

    @property
    def pid(self) -> int:
        return os.getpid()