"""backend.services.agent.core (llm / fs / text) 测试。"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- llm


def test_resolve_base_url():
    from backend.services.agent.core.llm import resolve_base_url

    cfg = _cfg()
    cfg.llm_base_url = " https://gate.example.com/v1/ "
    assert resolve_base_url(cfg) == "https://gate.example.com/v1"
    cfg.llm_base_url = ""
    cfg.llm_provider = "deepseek"
    assert resolve_base_url(cfg) == "https://api.deepseek.com/v1"
    cfg.llm_provider = "dashscope"
    assert resolve_base_url(cfg) == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    cfg.llm_provider = "openai"
    assert resolve_base_url(cfg) is None
    cfg.llm_provider = "unknown"
    assert resolve_base_url(cfg) is None


def test_build_chat_model_no_key():
    from backend.services.agent.core.llm import build_chat_model

    cfg = _cfg()
    cfg.llm_api_key = ""
    with pytest.raises(RuntimeError, match="LLM API Key"):
        build_chat_model(cfg)
    cfg.llm_api_key = "   "
    with pytest.raises(RuntimeError, match="LLM API Key"):
        build_chat_model(cfg)


def test_build_chat_model(monkeypatch):
    from backend.services.agent.core.llm import build_chat_model

    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    # langchain_openai 在 build_chat_model 内部 import
    import langchain_openai as _lo

    monkeypatch.setattr(_lo, "ChatOpenAI", FakeChatOpenAI)

    cfg = _cfg()
    cfg.llm_api_key = "sk-123"
    cfg.llm_model = "deepseek-v4-flash"
    cfg.llm_provider = "deepseek"
    cfg.llm_temperature = 0.5
    model = build_chat_model(cfg)
    assert model is not None
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["temperature"] == 0.5
    assert captured["base_url"] == "https://api.deepseek.com/v1"


# --------------------------------------------------------------------------- fs


def test_agent_fs_paths():
    from backend.services.agent.core.fs import (
        AGENT_BACKEND_DIR,
        AGENT_SAVED_DIR,
        agent_real_path,
        agent_virtual_path,
    )

    real = agent_real_path(AGENT_SAVED_DIR)
    assert str(real).endswith("agent_saved")
    real2 = agent_real_path(AGENT_BACKEND_DIR)
    assert str(real2).endswith("agent_backend")

    # 虚拟路径 <-> 真实路径互转
    vp = agent_virtual_path(real / "x.txt")
    assert vp == "/agent_saved/x.txt"
    assert agent_real_path(vp) == real / "x.txt"


def test_agent_virtual_path_outside_root():
    from backend.services.agent.core.fs import agent_virtual_path

    # tmp 目录之外的路径回退为文件名
    out = agent_virtual_path("/etc/passwd")
    assert out == "passwd"


def test_agent_sanitize():
    from backend.services.agent.core.fs import AGENT_TMP_ROOT, agent_sanitize

    real = str(AGENT_TMP_ROOT)
    text = f"路径: {real}/saved/content.txt 完成"
    out = agent_sanitize(text)
    assert real not in out
    assert "/saved/content.txt" in out


# --------------------------------------------------------------------------- text


def test_cap_text():
    from backend.services.agent.core.text import cap_text

    assert cap_text("hello", 10) == "hello"
    assert cap_text("hello", 3) == "hel\n...[已截断, 共 5 字符]"
    assert cap_text(12345, 100) == "12345"
    assert cap_text(None, 100) == "None"


def _cfg():
    from backend.config import Config

    return Config()
