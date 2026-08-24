"""backend.services.capture 测试。"""

from __future__ import annotations

import asyncio

import pytest


def test_subscriber_push_wait():
    from backend.services.capture import Subscriber

    sub = Subscriber()
    sub.push((b"frame1", 1.0))
    assert asyncio.run(sub.wait(timeout=1)) == (b"frame1", 1.0)
    # 缓冲取空后 wait 超时会抛 asyncio.TimeoutError(由调用方捕获)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(sub.wait(timeout=0.05))


async def test_subscriber_event_cleared():
    from backend.services.capture import Subscriber

    sub = Subscriber()
    sub.push((b"a", 1.0))
    item = await sub.wait(timeout=1)
    assert item == (b"a", 1.0)
    # 事件已清除且缓冲为空 -> 超时抛 TimeoutError
    with pytest.raises(asyncio.TimeoutError):
        await sub.wait(timeout=0.05)


def test_subscriber_overwrites_buffer():
    from backend.services.capture import Subscriber

    sub = Subscriber()
    sub.push((b"old", 1.0))
    sub.push((b"new", 2.0))
    assert asyncio.run(sub.wait(timeout=1)) == (b"new", 2.0)


async def test_screen_capture_status_and_attach(cfg):
    from backend.services.capture import ScreenCapture, Subscriber

    cap = ScreenCapture(cfg)
    assert cap.status()["viewers"] == 0
    sub = Subscriber()
    cap.attach(sub)
    assert cap.status()["viewers"] == 1
    assert cap._has_subs()
    cap.detach(sub)
    assert cap.status()["viewers"] == 0
    cap.detach(sub)  # 重复 detach 不报错


def test_deliver(cfg):
    from backend.services.capture import ScreenCapture

    cap = ScreenCapture(cfg)
    cap._deliver(b"jpeg", 100.0)
    assert cap.latest == (b"jpeg", 100.0)
    assert cap.frames_total == 1
    assert cap.status()["running"] is False
    assert cap.status()["frames_total"] == 1
    assert cap.status()["last_frame_age_ms"] is not None


async def test_capture_loop_single_frame(cfg, monkeypatch):
    from backend.services.capture import ScreenCapture

    cap = ScreenCapture(cfg)
    cap._image_grab = lambda xdisplay=None: _FakeImage()
    cap._deliver(b"jpeg", 1.0)
    # 手动跑一次循环(有订阅者)
    sub = _FakeSub()
    cap.attach(sub)
    task = asyncio.create_task(cap._capture_loop())
    await asyncio.sleep(0.05)
    cap._stop.set()
    await task
    assert sub.pushed >= 1
    assert cap.error is None


async def test_capture_loop_no_subs_sleep(cfg):
    from backend.services.capture import ScreenCapture

    cap = ScreenCapture(cfg)
    cap._warmup = False
    task = asyncio.create_task(cap._capture_loop())
    await asyncio.sleep(0.05)
    cap._stop.set()
    await task


async def test_capture_loop_error(cfg, monkeypatch):
    from backend.services.capture import ScreenCapture

    cap = ScreenCapture(cfg)

    def boom(*args, **kwargs):
        raise OSError("grab failed")

    cap._image_grab = boom
    task = asyncio.create_task(cap._capture_loop())
    await asyncio.sleep(0.05)
    assert cap.error is not None
    cap._stop.set()
    await task


async def test_wait_first_frame_timeout(cfg, monkeypatch):
    import backend.services.capture as capture_mod
    from backend.services.capture import ScreenCapture

    # 让 time.time 每次调用快速前进, 跳过 15s 真实等待
    fake = {"t": 1000.0}

    def _fast_time():
        fake["t"] += 1.0
        return fake["t"]

    monkeypatch.setattr(capture_mod.time, "time", _fast_time)
    cap = ScreenCapture(cfg)
    with pytest.raises(RuntimeError, match="等待抓屏首帧超时"):
        await cap._wait_first_frame()


async def test_wait_first_frame_error(cfg):
    from backend.services.capture import ScreenCapture

    cap = ScreenCapture(cfg)
    cap.error = "boom"
    with pytest.raises(RuntimeError, match="抓屏失败"):
        await cap._wait_first_frame()


async def test_wait_first_frame_ok(cfg):
    from backend.services.capture import ScreenCapture

    cap = ScreenCapture(cfg)
    cap._deliver(b"jpeg", 1.0)
    await cap._wait_first_frame()
    assert cap._warmup is False


async def test_start_stop(cfg, monkeypatch):
    from backend.services.capture import ScreenCapture

    cap = ScreenCapture(cfg)

    class FakeImage:
        def save(self, buf, fmt, quality=70, optimize=False):
            buf.write(b"jpeg-bytes")

    cap._image_grab = lambda xdisplay=None: FakeImage()
    cap._wait_first_frame = _no_self_first_frame  # type: ignore[assignment]

    await cap.start()
    assert cap._task is not None
    assert cap.status()["running"] is True
    await cap.stop()
    assert cap.status()["running"] is False


async def test_start_raises_on_grab_error(cfg):
    from backend.services.capture import ScreenCapture

    cap = ScreenCapture(cfg)

    async def fail_first_frame():
        cap.error = "no display"
        raise RuntimeError("no display")

    cap._wait_first_frame = fail_first_frame  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="no display"):
        await cap.start()


async def test_fps_status(cfg):
    from backend.services.capture import ScreenCapture

    cap = ScreenCapture(cfg)
    for i in range(10):
        cap._frame_times.append(1000.0 + i)
    cap._deliver(b"x", 1010.0)
    st = cap.status()
    assert st["fps"] is not None
    assert st["fps"] > 0


def test_attach_detach_suppress(cfg):
    from backend.services.capture import ScreenCapture, Subscriber

    cap = ScreenCapture(cfg)
    sub = Subscriber()
    cap.attach(sub)
    cap.detach(sub)
    cap.detach(sub)


class _FakeImage:
    def save(self, buf, fmt, quality=70, optimize=False):
        buf.write(b"jpeg-bytes")


class _FakeSub:
    def __init__(self):
        self.pushed = 0

    def push(self, item):
        self.pushed += 1


async def _fake_first_frame(self):
    self._warmup = False


async def _no_self_first_frame():
    pass
