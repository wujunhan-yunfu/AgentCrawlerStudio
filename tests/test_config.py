"""backend.config 测试。"""

from __future__ import annotations

import socket
from unittest import mock

import pytest


def test_config_defaults():
    from backend.config import Config

    c = Config()
    assert c.display == ":99"
    assert c.width == 1280
    assert c.height == 800
    assert c.framerate == 30
    assert c.jpeg_quality == 70
    assert c.cdp_port == 9222
    assert c.web_host == "0.0.0.0"
    assert c.web_port == 8080
    assert c.api_prefix == "/api/v1"
    assert c.chrome is None
    assert c.crawler_id == "dev_test"
    assert c.mongo_uri == ""
    assert c.mongo_db == "crawler"
    assert c.llm_provider == "deepseek"
    assert c.llm_model == "deepseek-v4-flash"
    assert c.llm_api_key == ""
    assert c.llm_base_url == ""
    assert c.llm_temperature == 0.2
    assert c.dev_limit is True
    assert c.max_items == 50
    assert c.max_bytes == 512 * 1024


def test_static_dir():
    from backend.config import STATIC_DIR

    assert STATIC_DIR.name == "static"


def test_find_chrome_found(monkeypatch):
    from backend.config import find_chrome

    monkeypatch.setattr(
        "backend.config.shutil.which",
        lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None,
    )
    assert find_chrome() == "/usr/bin/google-chrome"


def test_find_chrome_second(monkeypatch):
    from backend.config import find_chrome

    def fake_which(name):
        return "/usr/bin/chromium" if name == "chromium" else None

    monkeypatch.setattr("backend.config.shutil.which", fake_which)
    assert find_chrome() == "/usr/bin/chromium"


def test_find_chrome_missing(monkeypatch):
    from backend.config import find_chrome

    monkeypatch.setattr("backend.config.shutil.which", lambda name: None)
    with pytest.raises(SystemExit):
        find_chrome()


def test_find_free_port(monkeypatch):
    from backend.config import find_free_port

    class FakeSocket:
        def __init__(self, results):
            self._results = results

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return next(self._results)

        def close(self):
            pass

    # 端口空闲(connect_ex != 0) -> 直接返回 preferred
    monkeypatch.setattr(
        "backend.config.socket.socket", lambda *args: FakeSocket(iter([111]))
    )
    assert find_free_port(9222) == 9222

    # preferred 被占用(connect_ex == 0) -> 递增找到下一个空闲
    results = iter([0, 111])
    monkeypatch.setattr(
        "backend.config.socket.socket", lambda *args: FakeSocket(results)
    )
    assert find_free_port(9000) == 9001


def test_build_config_env(monkeypatch):
    import backend.config as config_mod
    from backend.config import Config, build_config

    monkeypatch.setattr(
        config_mod,
        "PROJECT_ROOT",
        config_mod.PROJECT_ROOT,
    )
    monkeypatch.setenv("XFB_DISPLAY", ":77")
    monkeypatch.setenv("XFB_WIDTH", "1024")
    monkeypatch.setenv("XFB_HEIGHT", "768")
    monkeypatch.setenv("FPS", "15")
    monkeypatch.setenv("JPEG_QUALITY", "50")
    monkeypatch.setenv("CDP_PORT", "9333")
    monkeypatch.setenv("WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("WEB_PORT", "9999")
    monkeypatch.setenv("API_PREFIX", "api/v2")
    monkeypatch.setenv("CHROME_PATH", "/opt/chrome")
    monkeypatch.setenv("CRAWLER_ID", "crawl_42")
    monkeypatch.setenv("MONGO_URI", "mongodb://x:1")
    monkeypatch.setenv("MONGO_DB", "mydb")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.com/v1/")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.9")
    monkeypatch.setenv("DEV_LIMIT", "0")
    monkeypatch.setenv("MAX_ITEMS", "5")
    monkeypatch.setenv("MAX_BYTES", "1024")
    monkeypatch.setattr("sys.argv", ["prog"])

    c = build_config()
    assert isinstance(c, Config)
    assert c.display == ":77"
    assert c.width == 1024
    assert c.height == 768
    assert c.framerate == 15
    assert c.jpeg_quality == 50
    assert c.cdp_port == 9333
    assert c.web_host == "127.0.0.1"
    assert c.web_port == 9999
    assert c.api_prefix == "/api/v2"
    assert c.chrome == "/opt/chrome"
    assert c.crawler_id == "crawl_42"
    assert c.mongo_uri == "mongodb://x:1"
    assert c.mongo_db == "mydb"
    assert c.llm_provider == "openai"
    assert c.llm_model == "gpt-4o"
    assert c.llm_api_key == "sk-test"
    assert c.llm_base_url == "https://example.com/v1/"
    assert c.llm_temperature == 0.9
    assert c.dev_limit is False
    assert c.max_items == 5
    assert c.max_bytes == 1024


def test_build_config_cli_args(monkeypatch):
    from backend.config import build_config

    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--display", ":55",
            "--width", "800",
            "--height", "600",
            "--api-prefix", "foo",
            "--no-dev-limit",
        ],
    )
    c = build_config()
    assert c.display == ":55"
    assert c.width == 800
    assert c.height == 600
    assert c.api_prefix == "/foo"
    assert c.dev_limit is False
