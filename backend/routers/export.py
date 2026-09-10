"""导出脚本路由: 把编辑器代码打包为可 uv 运行的独立工程。

- POST /code/validate-cron   校验 cron 表达式, 返回接下来 5 次运行时间
- POST /code/export          导出独立包(zip 下载)
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..schemas import ExportRequest, ValidateCronRequest, ValidateCronResult
from ..services.cron import CronError, next_runs, validate_cron
from ..services.exporter import build_package

router = APIRouter(tags=["code"])


@router.post("/code/validate-cron", response_model=ValidateCronResult)
async def validate_cron_expression(req: ValidateCronRequest) -> dict:
    """校验 cron 表达式, 合法时返回接下来 5 次运行时间(毫秒时间戳)。"""
    ok, error = validate_cron(req.expression)
    runs: list[int] = []
    if ok:
        try:
            runs = [int(t.timestamp() * 1000) for t in next_runs(req.expression, count=5)]
        except CronError as exc:
            ok, error = False, str(exc)
    return {"ok": ok, "error": error, "next_runs": runs}


@router.post("/code/export")
async def export_code(req: ExportRequest) -> Response:
    """把编辑器代码导出为独立 zip 包。"""
    if not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="编辑器代码为空, 无法导出")
    try:
        filename, data = build_package(
            req.code,
            name=req.name or "",
            cron=req.cron or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    disposition = (
        f"attachment; filename=\"{filename}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
    )
