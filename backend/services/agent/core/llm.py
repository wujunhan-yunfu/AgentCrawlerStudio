"""爬虫 Agent 的 LLM 构建: 基于配置创建 OpenAI 兼容聊天模型。

支持任意 OpenAI 兼容接口(deepseek / dashscope / openai / 自建网关),
通过 config 中的 provider / base_url / api_key / model / temperature 控制。
"""

from __future__ import annotations

from ....config import Config

_PROVIDER_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openai": None,
}


def resolve_base_url(cfg: Config) -> str | None:
    """按 provider 推断默认 base URL, 用户显式指定时优先。"""
    if cfg.llm_base_url.strip():
        return cfg.llm_base_url.strip().rstrip("/")
    return _PROVIDER_BASE_URLS.get(cfg.llm_provider)


def build_chat_model(cfg: Config):
    """构建 ChatOpenAI 实例。

    Raises:
        RuntimeError: 未配置 API Key 或 provider 不支持。
    """
    api_key = cfg.llm_api_key.strip()
    if not api_key:
        raise RuntimeError(
            "未配置 LLM API Key, 无法启动爬虫 Agent。"
            "请在启动后端时通过 --llm-api-key 或环境变量 LLM_API_KEY 指定"
            "(示例: --llm-api-key sk-xxx --llm-model deepseek-chat)。"
        )
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    return ChatOpenAI(
        model=cfg.llm_model,
        api_key=SecretStr(api_key),
        base_url=resolve_base_url(cfg),
        temperature=cfg.llm_temperature,
        timeout=120,
        max_retries=2,
    )
