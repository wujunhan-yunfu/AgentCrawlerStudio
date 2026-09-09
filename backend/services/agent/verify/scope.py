"""产物运行期: `verify_check` 常驻监听的作用域实现。

语义(docs/human-verification-solution.md §7.2/§7.4):
- 进入 async with 启动常驻 watcher(低频轮询证据, 出现验证即自动解);
- 单次验证触发按 attempts 计预算, 预算耗尽仍未过 → 记 failure;
- failure 在 vc.passed() 或 __aexit__(出口屏障) 处抛 VerificationFailed 结束本次运行;
- 全程不弹窗、不依赖人工 gate。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .classify import build_classifier
from .coordinator import VerificationCoordinator, build_coordinator, should_engage
from .evidence import parse_evidence
from .exceptions import VerificationFailed
from .ops import PlaywrightOps

log = logging.getLogger(__name__)

_POLL = 0.6  # 低频兜底轮询间隔(秒)


class VerifyScope:
    """由 CrawlerEnv.verify_check() 返回的异步上下文管理器。"""

    def __init__(
        self,
        cfg: Any,
        page: Any,
        *,
        timeout: float = 0,
        attempts: int | None = None,
        wait_on_exit: bool = True,
        hints: dict[str, Any] | None = None,
        classifier: Any = None,
    ):
        self.cfg = cfg
        self.page = page
        self.timeout = max(0.0, float(timeout))
        self.attempts = int(attempts) if attempts else int(
            getattr(cfg, "verify_max_attempts", 3) or 3
        )
        self.wait_on_exit = wait_on_exit
        self.hints = hints or {}
        self._classifier = classifier
        self._coordinator: VerificationCoordinator | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._watcher: asyncio.Task | None = None
        self._failure: dict[str, Any] | None = None
        self.last: dict[str, Any] | None = None
        self.episodes = 0  # 本次作用域成功处理/通过的验证次数

    # ------------------------------------------------------------------ 生命周期

    def _coord(self) -> VerificationCoordinator:
        if self._coordinator is None:
            ops = PlaywrightOps(self.page)
            clf = self._classifier or build_classifier(self.cfg)
            self._coordinator = VerificationCoordinator(
                ops,
                clf,
                slider_only_right=bool(getattr(self.cfg, "verify_slider_only_right", True)),
            )
        return self._coordinator

    async def __aenter__(self) -> "VerifyScope":
        self._stop = asyncio.Event()
        self._failure = None
        loop = asyncio.get_running_loop()
        deadline = (loop.time() + self.timeout) if self.timeout > 0 else None
        self._watcher = loop.create_task(self._watch(deadline))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._stop.set()
        w = self._watcher
        self._watcher = None
        if w is not None and not w.done():
            w.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await w
        # 出口屏障: 若失败已发生或仍被拦截, 在剩余预算内再解; 否则结束本次运行
        if exc_type is not None:
            # 主体代码本身抛错 -> 透传, 不做验证收尾(避免掩盖业务错误)
            return False
        if self._failure is not None:
            self._raise_failed()
        if not self.wait_on_exit:
            return False
        ev = await self._coord().collect_evidence()
        if should_engage(ev):
            await self._solve_episode()
        if self._failure is not None:
            self._raise_failed()
        return False

    # ------------------------------------------------------------------ 监听

    async def _watch(self, deadline: float | None) -> None:
        coord = self._coord()
        try:
            while not self._stop.is_set():
                if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    return
                try:
                    ev = parse_evidence(await coord.collect_evidence())
                except Exception:  # noqa: BLE001
                    ev = {}
                if not should_engage(ev):
                    await asyncio.sleep(_POLL)
                    continue
                # 已在出口/显式 passed 里被处理则跳过
                if self._failure is not None:
                    return
                await self._solve_episode()
                if self._failure is not None:
                    return
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("verify_check watcher 异常退出")

    async def _solve_episode(self) -> None:
        """加锁执行一次"触发→自动解"闭环(供 watcher/passed/exit 共用)。"""
        async with self._lock:
            if self._failure is not None:
                return
            try:
                result = await self._coord().resolve(attempts=self.attempts)
            except Exception as exc:  # noqa: BLE001
                self._failure = {"ok": False, "attempts": self.attempts,
                                 "type": "unknown", "reason": f"{type(exc).__name__}: {exc}"}
                return
            self.last = result
            if result.get("ok"):
                self.episodes += 1
            else:
                self._failure = result

    # ------------------------------------------------------------------ 协作接口

    async def passed(self) -> bool:
        """显式等待"绿灯"(当前无验证 / 验证已通过)。失败则抛 VerificationFailed。"""
        if self._failure is not None:
            self._raise_failed()
        ev = parse_evidence(await self._coord().collect_evidence())
        if should_engage(ev):
            await self._solve_episode()
        if self._failure is not None:
            self._raise_failed()
        return True

    def status(self) -> dict[str, Any]:
        return {
            "running": self._watcher is not None and not self._watcher.done(),
            "attempts": self.attempts,
            "episodes": self.episodes,
            "failure": self._failure,
            "last": self.last,
        }

    def _runtime_exit(self) -> bool:
        return bool(getattr(self.cfg, "verify_runtime_exit", True))

    def _raise_failed(self) -> None:
        r = self._failure or {}
        if not self._runtime_exit():
            # 调试模式(verify_runtime_exit=False): 不结束运行, 结果记录在 status()/last
            log.warning("verify_check 未通过但 runtime_exit=False: %s", r)
            return
        raise VerificationFailed(
            f"人机验证在 {r.get('attempts', self.attempts)} 次内未通过"
            f"({r.get('type') or 'unknown'}): {r.get('reason') or '仍在拦截'}",
            type=r.get("type") or "", attempts=r.get("attempts") or 0,
            reason=r.get("reason") or "",
        )
