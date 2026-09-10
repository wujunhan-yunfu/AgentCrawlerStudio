"""backend.routers.export 测试: cron 校验与脚本导出。"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from conftest import make_test_app


@pytest.fixture()
async def client():
    app = make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_validate_cron_ok(client):
    resp = await client.post("/api/v1/code/validate-cron", json={"expression": "*/5 * * * *"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] == ""
    assert len(body["next_runs"]) == 5


async def test_validate_cron_invalid(client):
    resp = await client.post("/api/v1/code/validate-cron", json={"expression": "bad"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]
    assert body["next_runs"] == []


async def test_export_ok(client):
    resp = await client.post(
        "/api/v1/code/export",
        json={"code": "print('hi')\n", "name": "demo", "cron": "*/5 * * * *"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "demo.zip" in resp.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert "demo/main.py" in zf.namelist()


async def test_export_empty_code(client):
    resp = await client.post("/api/v1/code/export", json={"code": "   ", "name": "demo"})
    assert resp.status_code == 400


async def test_export_invalid_cron(client):
    resp = await client.post(
        "/api/v1/code/export",
        json={"code": "print('hi')\n", "name": "demo", "cron": "nope"},
    )
    assert resp.status_code == 400
