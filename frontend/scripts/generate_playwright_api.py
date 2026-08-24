#!/usr/bin/env python
"""从安装的 playwright 包提取 async API 索引, 供前端 Monaco 代码辅助使用。

用法: .venv/bin/python frontend/scripts/generate_playwright_api.py
输出: frontend/src/playwrightApi.json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

MAX_DOC_CHARS = 600


def clean_doc(node: ast.AST | None) -> str:
    if not node:
        return ""
    doc = ast.get_docstring(node, clean=False)
    if not doc:
        return ""
    doc = re.sub(r"\s+", " ", doc).strip()
    if len(doc) > MAX_DOC_CHARS:
        doc = doc[:MAX_DOC_CHARS].rstrip() + "..."
    return doc


def unparse_default(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return None


def sig_params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict]:
    params: list[dict] = []
    args = func.args
    all_args: list[tuple[str, ast.AST | None, bool]] = []
    for a in args.posonlyargs:
        all_args.append((a.arg, a.annotation, False))
    for a in args.args:
        all_args.append((a.arg, a.annotation, False))
    if args.vararg:
        all_args.append((f"*{args.vararg.arg}", args.vararg.annotation, False))
    kwonly_start = len(all_args)
    for a in args.kwonlyargs:
        all_args.append((a.arg, a.annotation, True))
    if args.kwarg:
        all_args.append((f"**{args.kwarg.arg}", args.kwarg.annotation, True))

    defaults = [None] * (len(args.posonlyargs) + len(args.args) - len(args.defaults)) + list(
        args.defaults
    )
    for idx, (name, ann, kwonly) in enumerate(all_args):
        is_vararg = name.startswith("*") and not name.startswith("**")
        default = None
        if not is_vararg and kwonly and len(args.kw_defaults) and idx >= kwonly_start:
            kw_idx = idx - kwonly_start
            if kw_idx < len(args.kw_defaults):
                default = unparse_default(args.kw_defaults[kw_idx])
        elif idx < len(defaults):
            default = unparse_default(defaults[idx])
        params.append(
            {
                "name": name,
                "kwonly": kwonly,
                "default": default,
                "type": ast.unparse(ann) if ann else "",
            }
        )
    return params


def normalize_ret(node: ast.AST | None) -> str:
    return ast.unparse(node) if node else "None"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=None, help="playwright async _generated.py 路径")
    parser.add_argument("--out", default=None, help="输出 JSON 路径")
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path(__file__).resolve().parent.parent / "src" / "playwrightApi.json"

    if args.source:
        source_path = Path(args.source)
    else:
        root = Path(__file__).resolve().parents[2]
        candidates = sorted((root / ".venv" / "lib").glob("python*/site-packages/playwright/async_api/_generated.py"))
        if not candidates:
            raise SystemExit("未找到 playwright _generated.py, 请用 --source 指定路径")
        source_path = candidates[-1]

    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    classes: dict[str, dict] = {}
    overload_decorator = "typing.overload"
    property_decorator = "property"

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        members: dict[str, dict] = {}
        for child in node.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = child.name
            if name.startswith("__") and name.endswith("__") and name != "__init__":
                continue
            is_overload = any(
                isinstance(d, ast.Name) and d.id == "overload"
                or isinstance(d, ast.Attribute) and d.attr == "overload"
                for d in child.decorator_list
            )
            is_property = any(
                isinstance(d, ast.Name) and d.id == property_decorator
                or isinstance(d, ast.Attribute) and d.attr == property_decorator
                for d in child.decorator_list
            )
            if is_overload:
                existing = members.get(name)
                if existing is None:
                    members[name] = {
                        "kind": "method",
                        "async": isinstance(child, ast.AsyncFunctionDef),
                        "params": sig_params(child),
                        "ret": normalize_ret(child.returns),
                        "doc": clean_doc(child),
                    }
                continue
            members[name] = {
                "kind": "property" if is_property else "method",
                "async": isinstance(child, ast.AsyncFunctionDef),
                "params": [] if is_property else sig_params(child),
                "ret": normalize_ret(child.returns),
                "doc": clean_doc(child),
            }
        classes[node.name] = {
            "bases": [base.id if isinstance(base, ast.Name) else "" for base in node.bases],
            "doc": clean_doc(node),
            "members": members,
        }

    out.write_text(json.dumps({"version": "1.0", "classes": classes}, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.1f} KB, {len(classes)} classes)")


if __name__ == "__main__":
    main()
