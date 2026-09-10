"""backend.services.exporter 测试: 导出独立包的内容与结构。"""

from __future__ import annotations

import io
import py_compile
import zipfile

import pytest

from backend.services.exporter import build_package, safe_name


def _zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def test_safe_name():
    assert safe_name("my crawler!!") == "my_crawler"
    assert safe_name("  ") == "crawler"
    assert safe_name("a-b_c") == "a-b_c"
    assert safe_name("../../etc") == "etc"


def test_build_package_structure():
    name, data = build_package("print('hi')\n", name="demo")
    assert name == "demo.zip"
    zf = _zip(data)
    names = set(zf.namelist())
    assert names == {
        "demo/pyproject.toml",
        "demo/README.md",
        "demo/main.py",
        "demo/runtime.py",
        "demo/cron.py",
        "demo/crawler.py",
        "demo/.gitignore",
    }
    assert zf.read("demo/crawler.py").decode() == "print('hi')\n"
    assert "playwright" in zf.read("demo/pyproject.toml").decode()


def test_build_package_embeds_cron():
    _, data = build_package("x = 1\n", name="demo", cron="*/5 * * * *")
    zf = _zip(data)
    main = zf.read("demo/main.py").decode()
    assert 'DEFAULT_CRON = "*/5 * * * *"' in main
    assert "*/5 * * * *" in zf.read("demo/README.md").decode()


def test_build_package_empty_cron_placeholder():
    _, data = build_package("x = 1\n", name="demo")
    main = _zip(data).read("demo/main.py").decode()
    assert 'DEFAULT_CRON = ""' in main


def test_build_package_invalid_cron():
    with pytest.raises(ValueError):
        build_package("x = 1\n", name="demo", cron="not a cron")


def test_generated_python_compiles(tmp_path):
    _, data = build_package("await page.goto('https://example.com')\n", name="demo")
    _zip(data).extractall(tmp_path)
    for fname in ("main.py", "runtime.py", "cron.py"):
        py_compile.compile(str(tmp_path / "demo" / fname), doraise=True)
