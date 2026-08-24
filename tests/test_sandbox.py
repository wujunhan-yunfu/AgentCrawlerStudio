"""backend.services.sandbox 测试。"""

from __future__ import annotations

import pytest


def test_safe_builtins_no_open():
    from backend.services.sandbox import safe_builtins

    b = safe_builtins()
    assert "open" not in b
    assert "__import__" in b


def test_guarded_import_blocks():
    from backend.services.sandbox import _guarded_import

    for mod in ("os", "pathlib", "shutil", "subprocess", "io", "sys", "builtins"):
        with pytest.raises(ImportError):
            _guarded_import(mod)


def test_guarded_import_passthrough():
    from backend.services.sandbox import _guarded_import

    import json as _json

    mod = _guarded_import("json")
    assert mod is _json
    # 嵌套模块名只检查根
    mod = _guarded_import("json.decoder")
    assert mod is _json


def test_exec_sandbox_blocks_open():
    from backend.services.sandbox import safe_builtins

    env = {"__builtins__": safe_builtins(), "__name__": "__main__"}
    with pytest.raises(ImportError):
        exec("import os", env)


def test_exec_sandbox_usable():
    from backend.services.sandbox import safe_builtins

    env = {"__builtins__": safe_builtins(), "__name__": "__main__", "acc": []}
    exec("acc.append(sum([1, 2, 3]))", env)
    assert env["acc"] == [6]
