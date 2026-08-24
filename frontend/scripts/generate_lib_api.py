#!/usr/bin/env python
"""从已安装的爬虫/后处理库提取公开 API 索引, 供前端 Monaco 代码辅助使用。

用法: .venv/bin/python frontend/scripts/generate_lib_api.py
输出: frontend/src/libApi.json

默认索引: httpx / bs4 / bs4.element / lxml / lxml.etree / re / json / urllib.parse
可用 --modules 指定其他模块。
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import typing
from pathlib import Path

MAX_DOC_CHARS = 200

DEFAULT_MODULES = [
    "httpx",
    "bs4",
    "bs4.element",
    "lxml",
    "lxml.etree",
    "re",
    "json",
    "urllib.parse",
    "urllib.request",
    "os",
    "os.path",
    "sys",
    "time",
    "datetime",
    "math",
    "random",
    "pathlib",
    "html",
    "hashlib",
    "base64",
    "csv",
    "collections",
    "itertools",
    "functools",
    "subprocess",
    "glob",
    "shutil",
    "string",
]

# 个别常用方法的返回类型注解缺失或过于笼统(typing 别名), 手工补全以便前端类型链推断
RET_OVERRIDES = {
    "bs4.BeautifulSoup.find": "bs4.element.Tag",
    "bs4.BeautifulSoup.find_all": "List[bs4.element.Tag]",
    "bs4.BeautifulSoup.select": "List[bs4.element.Tag]",
    "bs4.BeautifulSoup.select_one": "bs4.element.Tag",
    "bs4.BeautifulSoup.get_text": "str",
    "bs4.element.Tag.find": "bs4.element.Tag",
    "bs4.element.Tag.find_all": "List[bs4.element.Tag]",
    "bs4.element.Tag.select": "List[bs4.element.Tag]",
    "bs4.element.Tag.select_one": "bs4.element.Tag",
    "bs4.element.Tag.get_text": "str",
    "bs4.element.Tag.get": "str",
    "bs4.element.NavigableString.get_text": "str",
    "lxml.etree.ElementBase.iterfind": "Iterator[lxml.etree.ElementBase]",
    "lxml.etree.ElementBase.findall": "List[lxml.etree.ElementBase]",
    "lxml.etree.ElementBase.find": "lxml.etree.ElementBase",
    "lxml.etree.ElementBase.xpath": "List[Any]",
    "datetime.datetime.now": "datetime.datetime",
    "datetime.datetime.today": "datetime.datetime",
    "datetime.datetime.utcnow": "datetime.datetime",
    "datetime.datetime.fromtimestamp": "datetime.datetime",
    "datetime.datetime.fromisoformat": "datetime.datetime",
    "datetime.datetime.strptime": "datetime.datetime",
    "datetime.datetime.date": "datetime.date",
    "datetime.datetime.time": "datetime.time",
    "datetime.datetime.astimezone": "datetime.datetime",
    "datetime.datetime.replace": "datetime.datetime",
    "datetime.date.today": "datetime.date",
    "datetime.date.fromtimestamp": "datetime.date",
    "datetime.date.fromisoformat": "datetime.date",
    "re.compile": "re.Pattern",
    "re.Pattern.match": "re.Match",
    "re.Pattern.search": "re.Match",
    "re.Pattern.fullmatch": "re.Match",
    "re.Pattern.findall": "List[str]",
    "re.Pattern.finditer": "Iterator[re.Match]",
    "re.Pattern.split": "List[str]",
    "re.Pattern.sub": "str",
    "re.Pattern.subn": "Tuple[str, int]",
    "re.match": "re.Match",
    "re.search": "re.Match",
    "re.fullmatch": "re.Match",
    "re.findall": "List[str]",
    "re.finditer": "Iterator[re.Match]",
    "re.sub": "str",
    "urllib.parse.urlparse": "urllib.parse.ParseResult",
    "urllib.parse.urlsplit": "urllib.parse.SplitResult",
    "urllib.parse.urljoin": "str",
    "urllib.parse.urlencode": "str",
    "urllib.parse.quote": "str",
    "urllib.parse.unquote": "str",
}

# 实例属性(仅 __init__ 中赋值, 不在 dir(cls) 里)的补充: 类名 -> {属性名: 类型}
PROPERTY_OVERRIDES = {
    "httpx.Response": {"status_code": "int", "url": "httpx.URL", "request": "httpx.Request"},
    "httpx.Request": {"url": "httpx.URL", "method": "str"},
    "bs4.element.Tag": {"attrs": "Dict[str, str]", "name": "str"},
    "datetime.datetime": {
        "year": "int",
        "month": "int",
        "day": "int",
        "hour": "int",
        "minute": "int",
        "second": "int",
        "microsecond": "int",
        "tzinfo": "datetime.tzinfo",
        "fold": "int",
    },
    "datetime.date": {"year": "int", "month": "int", "day": "int"},
    "datetime.time": {"hour": "int", "minute": "int", "second": "int", "microsecond": "int", "tzinfo": "datetime.tzinfo"},
    "datetime.timedelta": {"days": "int", "seconds": "int", "microseconds": "int"},
    "re.Match": {
        "pos": "int",
        "endpos": "int",
        "lastindex": "int",
        "lastgroup": "str",
        "re": "re.Pattern",
        "string": "str",
    },
    "re.Pattern": {"flags": "int", "groups": "int", "groupindex": "Dict[str, int]"},
}


def clean_doc(obj) -> str:
    doc = inspect.getdoc(obj)
    if not doc:
        return ""
    doc = re.sub(r"\s+", " ", doc).strip()
    if len(doc) > MAX_DOC_CHARS:
        doc = doc[:MAX_DOC_CHARS].rstrip() + "..."
    return doc


def is_async(fn) -> bool:
    try:
        return inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn)
    except Exception:  # noqa: BLE001
        return False


def type_str(ann) -> str:
    """把注解对象转成前端可解析的字符串(类名带模块前缀, 避免跨库重名)。"""
    if ann is None or ann is inspect.Signature.empty:
        return ""
    if isinstance(ann, str):
        return ann
    origin = typing.get_origin(ann)
    args = typing.get_args(ann)
    if origin is not None:
        oname = getattr(origin, "__name__", None) or str(origin).replace("typing.", "")
        if oname == "Union":
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                inner = type_str(non_none[0])
                return f"Optional[{inner}]" if inner else "None"
            inner = ", ".join(t for t in (type_str(a) for a in args) if t)
            return f"Union[{inner}]"
        inner = ", ".join(t for t in (type_str(a) for a in args) if t)
        return f"{oname}[{inner}]" if inner else oname
    if ann is type(None):
        return "None"
    if isinstance(ann, type):
        mod = ann.__module__
        return f"{mod}.{ann.__name__}" if mod and mod != "builtins" else ann.__name__
    s = str(ann)
    s = s.replace("typing.", "").replace("<class '", "").replace("'>", "")
    return s


def ret_str(fn) -> str:
    try:
        return type_str(inspect.signature(fn).return_annotation)
    except (ValueError, TypeError):
        return ""


def sig_params(fn) -> list[dict]:
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return []
    params: list[dict] = []
    for name, p in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        display = name
        if p.kind == inspect.Parameter.VAR_POSITIONAL:
            display = "*" + name
        elif p.kind == inspect.Parameter.VAR_KEYWORD:
            display = "**" + name
        default = None
        if p.default is not inspect.Signature.empty:
            try:
                default = repr(p.default)
            except Exception:  # noqa: BLE001
                default = None
        params.append(
            {
                "name": display,
                "kwonly": p.kind == inspect.Parameter.KEYWORD_ONLY,
                "default": default,
                "type": type_str(p.annotation),
            }
        )
    return params


def class_key(cls) -> str:
    mod = getattr(cls, "__module__", "") or ""
    name = getattr(cls, "__name__", "") or ""
    if not mod or mod == "builtins":
        return name
    return f"{mod}.{name}"


def class_entry(cls) -> dict:
    members: dict[str, dict] = {}
    init_params: list[dict] = []
    for name in dir(cls):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(cls, name)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(attr, property):
            fget = attr.fget
            members[name] = {
                "kind": "property",
                "async": is_async(fget) if fget else False,
                "params": [],
                "ret": ret_str(fget) if fget else "",
                "doc": clean_doc(fget) if fget else "",
            }
        elif inspect.isfunction(attr) or inspect.ismethod(attr) or callable(attr):
            members[name] = {
                "kind": "method",
                "async": is_async(attr),
                "params": sig_params(attr),
                "ret": ret_str(attr),
                "doc": clean_doc(attr),
            }
    try:
        init = getattr(cls, "__init__")
        if callable(init):
            init_params = sig_params(init)
    except Exception:  # noqa: BLE001
        pass

    entry: dict = {
        "bases": [getattr(b, "__name__", "") for b in getattr(cls, "__bases__", [])],
        "doc": clean_doc(cls),
        "members": members,
    }
    if init_params:
        entry["init"] = {"params": init_params, "doc": ""}
    return entry


def apply_overrides(modules: dict, classes: dict) -> None:
    for key, over in RET_OVERRIDES.items():
        cls_key, _, member = key.rpartition(".")
        entry = classes.get(cls_key)
        if entry and member in entry.get("members", {}):
            entry["members"][member]["ret"] = over
            continue
        mod = modules.get(cls_key)
        if mod and member in mod.get("members", {}):
            mod["members"][member]["ret"] = over
    for cls_key, props in PROPERTY_OVERRIDES.items():
        entry = classes.get(cls_key)
        if not entry:
            continue
        for name, typ in props.items():
            entry["members"].setdefault(
                name, {"kind": "property", "async": False, "params": [], "ret": typ, "doc": ""}
            )


def module_entry(mod, mod_name: str, classes: dict) -> dict:
    members: dict[str, dict] = {}
    for name in dir(mod):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(mod, name)
        except Exception:  # noqa: BLE001
            continue
        if inspect.ismodule(attr):
            sub_key = f"{mod_name}.{name}"
            members[name] = {
                "kind": "module",
                "async": False,
                "params": [],
                "ret": sub_key,
                "doc": clean_doc(attr),
            }
        elif inspect.isclass(attr):
            key = class_key(attr)
            if key not in classes:
                try:
                    classes[key] = class_entry(attr)
                except Exception:  # noqa: BLE001
                    classes[key] = {"bases": [], "doc": "", "members": {}}
            members[name] = {
                "kind": "class",
                "async": False,
                "params": [],
                "ret": key,
                "doc": clean_doc(attr),
            }
        elif inspect.isfunction(attr) or inspect.isbuiltin(attr) or callable(attr):
            members[name] = {
                "kind": "function",
                "async": is_async(attr),
                "params": sig_params(attr),
                "ret": ret_str(attr),
                "doc": clean_doc(attr),
            }
        else:
            # 模块级数据(常量/环境变量等), 补为只读属性
            try:
                type_name_str = type(attr).__name__
            except Exception:  # noqa: BLE001
                type_name_str = ""
            members[name] = {
                "kind": "property",
                "async": False,
                "params": [],
                "ret": type_name_str,
                "doc": clean_doc(attr),
            }
    return {"doc": clean_doc(mod), "members": members}


def add_parent_modules(modules: dict) -> None:
    """为带点的模块补全父模块引用, 如 lxml.etree -> lxml 模块含 etree 成员。"""
    for path in list(modules):
        parts = path.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            child = ".".join(parts[: i + 1])
            parent_entry = modules.setdefault(parent, {"doc": "", "members": {}})
            parent_entry["members"].setdefault(
                parts[i],
                {"kind": "module", "async": False, "params": [], "ret": child, "doc": ""},
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modules", default=",".join(DEFAULT_MODULES), help="逗号分隔的模块列表")
    parser.add_argument("--out", default=None, help="输出 JSON 路径")
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path(__file__).resolve().parent.parent / "src" / "libApi.json"
    module_names = [m.strip() for m in args.modules.split(",") if m.strip()]

    import importlib

    classes: dict[str, dict] = {}
    modules: dict[str, dict] = {}
    for name in module_names:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {name}: {exc}")
            continue
        modules[name] = module_entry(mod, name, classes)
        print(f"indexed {name}")

    apply_overrides(modules, classes)

    add_parent_modules(modules)
    out.write_text(
        json.dumps(
            {"version": "2.0", "modules": modules, "classes": classes},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"wrote {out} ({out.stat().st_size / 1024:.1f} KB, {len(modules)} modules, {len(classes)} classes)")


if __name__ == "__main__":
    main()
