"""backend.routers.console 与 backend.routers.lsp 测试。"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import WebSocketDisconnect

from conftest import FakeWS, make_test_app


# --------------------------------------------------------------------------- console


async def test_console_index():
    app = make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/")
        assert resp.status_code == 200
        assert b"<" in resp.content
        assert b'<meta name="acs-api-prefix" content="/api/v1">' in resp.content


def _index_asset_paths(content: bytes) -> list[str]:
    import re

    return re.findall(rb'(?:src|href)="([^"]+/assets/[^"]+)"', content)


async def test_console_index_web_prefix():
    from backend.config import Config

    cfg = Config(web_prefix="/studio")
    app = make_test_app(cfg=cfg)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        for path in ("/studio", "/studio/"):
            resp = await c.get(path)
            assert resp.status_code == 200, path
            assert b"<" in resp.content
            assets = _index_asset_paths(resp.content)
            assert assets, path
            for ref in assets:
                assert ref.startswith(b"/studio/assets/"), ref
        resp = await c.get("/studio/assets/not-exists-xyz.js")
        assert resp.status_code == 404


async def test_console_index_api_prefix_meta():
    from backend.config import Config

    cfg = Config(web_prefix="/studio", api_prefix="/api/studio/v1")
    app = make_test_app(cfg=cfg)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/studio/")
        assert resp.status_code == 200
        assert b'<meta name="acs-api-prefix" content="/api/studio/v1">' in resp.content
        assert b'="/assets/' not in resp.content
        r_new = await c.get("/api/studio/v1/status")
        assert r_new.status_code == 200
        r_old = await c.get("/api/v1/status")
        assert r_old.status_code == 404


async def test_console_assets_web_prefix():
    from backend.config import STATIC_DIR, Config

    assets = list((STATIC_DIR / "assets").glob("*")) if (STATIC_DIR / "assets").is_dir() else []
    if not assets:
        pytest.skip("static/assets 不存在")
    target = next(a for a in assets if a.is_file())
    cfg = Config(web_prefix="/studio")
    app = make_test_app(cfg=cfg)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r1 = await c.get(f"/studio/assets/{target.name}")
        assert r1.status_code == 200
        r2 = await c.get(f"/assets/{target.name}")
        assert r2.status_code == 200


# --------------------------------------------------------------------------- lsp info


async def test_lsp_info():
    app = make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/api/v1/lsp/info")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "pyright"
        assert "doc_uri" in body
        assert "python_path" in body


# --------------------------------------------------------------------------- lsp ws


class FakeLspSession:
    def __init__(self, ws):
        self.ws = ws
        self.started = False
        self.stopped = False
        self._exc = None

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def pump(self):
        if self._exc is not None:
            raise self._exc


class FakeLspManager:
    def __init__(self):
        self.created = []
        self.dropped = []

    async def create_session(self, ws):
        s = FakeLspSession(ws)
        self.created.append(s)
        return s

    def drop_session(self, session):
        self.dropped.append(session)


async def test_ws_lsp_disconnect(monkeypatch):
    import backend.routers.lsp as lsp_router

    fm = FakeLspManager()
    fm.created[0:0] = []
    monkeypatch.setattr(lsp_router, "_manager", fm)
    app = make_test_app()
    ws = FakeWS(app=app)
    session = fm.created  # ensure list exists before create
    # set pump to raise WebSocketDisconnect
    orig_create = fm.create_session

    async def create(ws_):
        s = await orig_create(ws_)
        s._exc = WebSocketDisconnect()
        return s

    fm.create_session = create
    await lsp_router.ws_lsp(ws)
    assert ws.accepted
    assert len(fm.created) == 1
    assert fm.created[0].started
    assert fm.created[0].stopped
    assert fm.dropped == [fm.created[0]]


async def test_ws_lsp_generic_exception(monkeypatch):
    import backend.routers.lsp as lsp_router

    fm = FakeLspManager()
    monkeypatch.setattr(lsp_router, "_manager", fm)
    orig_create = fm.create_session

    async def create(ws_):
        s = await orig_create(ws_)
        s._exc = RuntimeError("boom")
        return s

    fm.create_session = create
    app = make_test_app()
    ws = FakeWS(app=app)
    await lsp_router.ws_lsp(ws)
    assert ws.accepted
    assert fm.created[0].stopped
