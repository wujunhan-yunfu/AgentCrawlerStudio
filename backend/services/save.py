"""save_content 的多格式序列化辅助: txt / json / jsonl / csv / img。

- 默认格式为纯文本(txt)。
- img 格式传入 base64 图片字符串(data URI 或纯 base64), 文件后缀从
  base64 图片字符串的 mime 类型(如 data:image/png;base64,...)中读取。
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
from typing import Any

SAVE_FORMATS = ("txt", "json", "jsonl", "csv", "img")

_IMAGE_MIME_EXTS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/avif": ".avif",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

_DATA_URI_RE = re.compile(r"^data:([^;,]+)(?:;[^,]*)?,(.*)$", re.S)


def normalize_fmt(fmt: str) -> str:
    """规范化格式名, 未知格式抛错。"""
    fmt = (fmt or "txt").lower().lstrip(".")
    if fmt not in SAVE_FORMATS:
        raise ValueError(f"不支持的保存格式: {fmt!r}, 可选: {', '.join(SAVE_FORMATS)}")
    return fmt


def _to_json(data: Any) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2)


def _to_jsonl(data: Any) -> str:
    items = data if isinstance(data, list) else [data]
    lines = []
    for item in items:
        if isinstance(item, str):
            lines.append(item)
        else:
            lines.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(lines)


def _to_csv(data: Any) -> str:
    buf = io.StringIO()
    if isinstance(data, list) and data and isinstance(data[0], dict):
        keys = list(data[0].keys())
        writer = csv.DictWriter(buf, fieldnames=keys)
        writer.writeheader()
        for row in data:
            writer.writerow(row if isinstance(row, dict) else {k: "" for k in keys})
    elif isinstance(data, (list, tuple)):
        writer = csv.writer(buf)
        for row in data:
            if isinstance(row, (list, tuple)):
                writer.writerow(row)
            else:
                writer.writerow([row])
    else:
        raise ValueError("csv 格式需要传入 list[dict] 或 list[list]")
    return buf.getvalue()


def _parse_image(data: Any) -> tuple[bytes, str]:
    """解析 base64 图片, 返回 (解码字节, 文件扩展名)。

    支持 data URI(如 data:image/png;base64,xxxx)与纯 base64 字符串;
    后缀从字符串中的 mime 类型读取, 无法识别时回退 .png。
    """
    text = data if isinstance(data, str) else str(data)
    text = text.strip()
    mime = "image/png"
    b64 = text
    m = _DATA_URI_RE.match(text)
    if m:
        mime = m.group(1).strip().lower()
        b64 = m.group(2)
    ext = _IMAGE_MIME_EXTS.get(mime)
    if ext is None:
        ext = f".{mime.split('/')[-1]}" if mime.startswith("image/") else ".png"
    raw = base64.b64decode(b64)
    return raw, ext


def cap_text_bytes(text: str, max_bytes: int, notice: str = "") -> str:
    """按字节上限截断文本(保持 UTF-8 完整), 超出部分以 notice 标记替换。

    max_bytes <= 0 表示不限制。notice 会计入总字节预算。
    """
    if max_bytes <= 0:
        return text
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    tail = notice.encode("utf-8")
    budget = max_bytes - len(tail)
    if budget <= 0:
        return tail.decode("utf-8", errors="replace")
    data = data[:budget]
    while data:
        try:
            data.decode("utf-8")
            break
        except UnicodeDecodeError:
            data = data[:-1]
    return data.decode("utf-8") + notice


def prepare_save(data: Any, fmt: str, max_items: int | None = None,
                 max_bytes: int | None = None) -> tuple[str, bytes, str]:
    """按格式把数据转为可保存内容。

    返回 (扩展名, 写入字节, 展示文本):
    - txt/json/jsonl/csv 以 utf-8 字节写入, 展示文本即原文;
    - img 写入解码后的图片字节, 展示文本为原始 base64(data URI)。

    开发测试限制(max_items / max_bytes 非空时生效):
    - 列表/元组数据先截断为前 max_items 条, 保持各格式仍合法;
    - txt 格式再按 max_bytes 字节截断并追加截断标记。
    """
    fmt = normalize_fmt(fmt)
    if fmt == "img":
        raw, ext = _parse_image(data)
        display = data if isinstance(data, str) else str(data)
        return ext, raw, display
    if max_items and isinstance(data, (list, tuple)):
        data = data[:max_items]
    if fmt == "json":
        text = _to_json(data)
    elif fmt == "jsonl":
        text = _to_jsonl(data)
    elif fmt == "csv":
        text = _to_csv(data)
    else:
        text = data if isinstance(data, str) else str(data)
    ext = {"json": ".json", "jsonl": ".jsonl", "csv": ".csv"}.get(fmt, ".txt")
    if max_bytes and max_bytes > 0:
        text = cap_text_bytes(
            text, max_bytes,
            notice=f"\n...[已截断: 开发模式限制单次保存不超过 {max_bytes} 字节]",
        )
    return ext, text.encode("utf-8"), text
