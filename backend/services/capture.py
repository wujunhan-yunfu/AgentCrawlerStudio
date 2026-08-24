"""抓屏服务: asyncio 任务抓取 Xvfb 全屏 -> JPEG -> 扇出最新帧

    Pillow ImageGrab + JPEG 编码为同步阻塞库(无 async 版本),
    单帧抓取/编码放入默认执行器(asyncio.to_thread), 事件循环始终空闲。
"""

from __future__ import annotations

import asyncio
import collections
import io
import time
from contextlib import suppress


class Subscriber:
    """单客户端帧缓冲: 只留最新一帧 + asyncio 唤醒事件"""

    def __init__(self):
        self._buf: collections.deque = collections.deque(maxlen=1)
        self._event = asyncio.Event()

    def push(self, item: tuple[bytes, float]) -> None:
        self._buf.append(item)
        self._event.set()

    async def wait(self, timeout: float | None = None) -> tuple[bytes, float] | None:
        await asyncio.wait_for(self._event.wait(), timeout)
        self._event.clear()
        try:
            return self._buf.popleft()
        except IndexError:
            return None


class ScreenCapture:
    """asyncio 后台任务: 抓取 Xvfb 全屏 -> JPEG -> 扇出最新帧

    无人观看时降频抓取省 CPU; 有观看时按 framerate 连续抓屏,
    保证"末帧延迟"恒为 ~1 帧, 静态页面也不会出现延迟数字越滚越大。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.latest: tuple[bytes, float] | None = None
        self.frames_total = 0
        self._frame_times: collections.deque[float] = collections.deque(maxlen=240)
        self._subs: list[Subscriber] = []
        self._stop = asyncio.Event()
        self._warmup = True
        self._task: asyncio.Task | None = None
        self._image_grab = None
        self.error: str | None = None

    # ---------- 订阅 ----------
    def attach(self, sub: Subscriber) -> None:
        self._subs.append(sub)

    def detach(self, sub: Subscriber) -> None:
        with suppress(ValueError):
            self._subs.remove(sub)

    def _has_subs(self) -> bool:
        return bool(self._subs)

    def _viewers(self) -> int:
        return len(self._subs)

    # ---------- 抓屏循环 ----------
    async def start(self) -> None:
        from PIL import ImageGrab

        self._image_grab = ImageGrab.grab
        self._stop.clear()
        self._warmup = True
        self.frames_total = 0
        self._frame_times.clear()
        self.latest = None
        self.error = None
        self._task = asyncio.create_task(self._capture_loop())
        await self._wait_first_frame()

    def _grab_frame(self) -> bytes:
        img = self._image_grab(xdisplay=self.cfg.display)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=self.cfg.jpeg_quality, optimize=False)
        return buf.getvalue()

    async def _capture_loop(self) -> None:
        while not self._stop.is_set():
            if not self._has_subs() and not self._warmup:
                await asyncio.sleep(0.3)
                continue
            t0 = time.time()
            try:
                jpeg = await asyncio.to_thread(self._grab_frame)
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5)
                continue
            self.error = None
            self._deliver(jpeg, t0)
            elapsed = time.time() - t0
            frame_dt = 1.0 / self.cfg.framerate
            if elapsed < frame_dt:
                await asyncio.sleep(frame_dt - elapsed)

    def _deliver(self, jpeg: bytes, ts: float) -> None:
        self.latest = (jpeg, ts)
        self.frames_total += 1
        self._frame_times.append(ts)
        for sub in list(self._subs):
            sub.push((jpeg, ts))

    async def _wait_first_frame(self) -> None:
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.latest is not None:
                self._warmup = False
                return
            if self.error:
                raise RuntimeError(f"抓屏失败: {self.error}")
            await asyncio.sleep(0.1)
        self._warmup = False
        raise RuntimeError("等待抓屏首帧超时")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def status(self) -> dict:
        now = time.time()
        times = self._frame_times
        fps = None
        if len(times) >= 2:
            fps = round(len(times) / max(times[-1] - times[0], 1e-6), 1)
        return {
            "running": self._task is not None and not self._task.done(),
            "error": self.error,
            "viewers": self._viewers(),
            "fps": fps,
            "frames_total": self.frames_total,
            "last_frame_age_ms": round((now - self.latest[1]) * 1000, 1) if self.latest else None,
        }
