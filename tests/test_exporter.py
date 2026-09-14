"""backend.services.exporter 测试: 导出独立包的内容与结构。"""

from __future__ import annotations

import io
import py_compile
import zipfile

import pytest

from backend.services.exporter import (
    build_package,
    detect_dependencies,
    render_crawler_module,
    safe_name,
)


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
    crawler = zf.read("demo/crawler.py").decode()
    assert "async def run(" in crawler
    assert "print('hi')" in crawler
    assert "playwright" in zf.read("demo/pyproject.toml").decode()


def test_detect_dependencies():
    code = (
        "import json\n"
        "import os\n"
        "import bs4\n"
        "from PIL import Image\n"
        "from playwright.async_api import async_playwright\n"
        "from crawler import helper\n"
        "from . import local\n"
    )
    assert detect_dependencies(code) == ["beautifulsoup4", "pillow"]


def test_detect_dependencies_syntax_error():
    assert detect_dependencies("def (") == []


def test_build_package_appends_dependencies():
    _, data = build_package("import bs4\nfrom PIL import Image\n", name="demo")
    project = _zip(data).read("demo/pyproject.toml").decode()
    assert '"playwright>=1.40",' in project
    assert '"beautifulsoup4",' in project
    assert '"pillow",' in project


def test_render_crawler_module():
    src = render_crawler_module(
        "from __future__ import annotations\n\nawait page.goto('https://x')\n"
    )
    assert src.startswith("from __future__ import annotations\n")
    assert "async def run(" in src
    assert "await page.goto('https://x')" in src
    compile(src, "crawler.py", "exec")
    assert "    pass" in render_crawler_module("   \n")


async def test_generated_crawler_run_injects_globals(tmp_path):
    import importlib.util
    import sys

    _, data = build_package(
        "assert page == 'PAGE'\nresult = await save_content('hi', 'json')\n", name="demo"
    )
    _zip(data).extractall(tmp_path)
    sys.path.insert(0, str(tmp_path / "demo"))
    try:
        spec = importlib.util.spec_from_file_location(
            "demo_crawler", tmp_path / "demo" / "crawler.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls: dict = {}

        async def save_content(data, fmt="txt"):
            calls["data"], calls["fmt"] = data, fmt
            return "path"

        async def noop(*args, **kwargs):
            return None

        await module.run(
            page="PAGE",
            context=None,
            browser=None,
            save_page=noop,
            save_content=save_content,
            limit_items=lambda data, n=None: data,
            get_login_ticket=noop,
            set_login_ticket=noop,
            page_login=noop,
            verify_check=noop,
            VerificationFailed=Exception,
            capture_login_state=noop,
            restore_login_state=noop,
        )
        assert calls == {"data": "hi", "fmt": "json"}
    finally:
        sys.path.remove(str(tmp_path / "demo"))


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


def test_build_package_headless_default():
    _, data = build_package("x = 1\n", name="demo", headless=True)
    zf = _zip(data)
    assert "DEFAULT_HEADLESS = True" in zf.read("demo/main.py").decode()
    assert "无头模式" in zf.read("demo/README.md").decode()
    _, data2 = build_package("x = 1\n", name="demo", headless=False)
    assert "DEFAULT_HEADLESS = False" in _zip(data2).read("demo/main.py").decode()


def test_build_package_invalid_cron():
    with pytest.raises(ValueError):
        build_package("x = 1\n", name="demo", cron="not a cron")


def test_generated_python_compiles(tmp_path):
    _, data = build_package("await page.goto('https://example.com')\n", name="demo")
    _zip(data).extractall(tmp_path)
    for fname in ("main.py", "runtime.py", "cron.py", "crawler.py"):
        py_compile.compile(str(tmp_path / "demo" / fname), doraise=True)


def _load_runtime(tmp_path):
    import importlib.util
    import sys

    _, data = build_package("x = 1\n", name="demo")
    _zip(data).extractall(tmp_path)
    path = tmp_path / "demo" / "runtime.py"
    spec = importlib.util.spec_from_file_location("demo_runtime", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_runtime"] = module
    spec.loader.exec_module(module)
    return module


class _FakePage:
    def __init__(self, url: str = "https://example.com/login") -> None:
        self._url = url

    @property
    def url(self) -> str:
        return self._url

    async def goto(self, url: str, **kwargs) -> None:
        self._url = url


async def test_generated_page_login_invalid_method(tmp_path):
    runtime = _load_runtime(tmp_path)
    env = runtime.RuntimeEnv(_FakePage(), None, None, headless=True)
    result = await env.page_login("auto")
    assert result["ok"] is False
    assert "method" in result["error"]


async def test_generated_page_login_detects_redirect(tmp_path):
    runtime = _load_runtime(tmp_path)
    page = _FakePage("https://example.com/login")

    async def _go_home() -> None:
        import asyncio

        await asyncio.sleep(0.2)
        page._url = "https://example.com/home"

    import asyncio

    task = asyncio.create_task(_go_home())
    env = runtime.RuntimeEnv(page, None, None)
    result = await env.page_login("qr", timeout=5)
    await task
    assert result["ok"] is True
    assert result["url"] == "https://example.com/home"
