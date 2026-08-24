"""配置层: 应用/进程运行参数、路径与工具函数"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"


@dataclass
class Config:
    display: str = ":99"
    width: int = 1280
    height: int = 800
    framerate: int = 30
    jpeg_quality: int = 70
    cdp_port: int = 9222
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    api_prefix: str = "/api/v1"
    chrome: str | None = None
    crawler_id: str = "dev_test"
    mongo_uri: str = ""
    mongo_db: str = "crawler"
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-v4-flash"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_temperature: float = 0.2
    dev_limit: bool = True
    max_items: int = 50
    max_bytes: int = 512 * 1024


def find_chrome() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    raise SystemExit("未找到 Chrome/Chromium, 请通过 --chrome 指定路径")


def find_free_port(preferred: int) -> int:
    port = preferred
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
        port += 1


def build_config() -> Config:
    parser = argparse.ArgumentParser(description="Xvfb + Chrome(有头真实窗口) + 抓屏 实时画面 + Playwright 控制")
    parser.add_argument("--display", default=os.environ.get("XFB_DISPLAY", ":99"))
    parser.add_argument("--width", type=int, default=int(os.environ.get("XFB_WIDTH", "1280")))
    parser.add_argument("--height", type=int, default=int(os.environ.get("XFB_HEIGHT", "800")))
    parser.add_argument("--framerate", type=int, default=int(os.environ.get("FPS", "30")),
                        help="抓屏帧率上限(受编码耗时约束, 实际约 30fps)")
    parser.add_argument("--quality", type=int, default=int(os.environ.get("JPEG_QUALITY", "70")),
                        help="JPEG 画质 1-100, 越高越清晰但带宽越大")
    parser.add_argument("--cdp-port", type=int, default=int(os.environ.get("CDP_PORT", "9222")))
    parser.add_argument("--host", default=os.environ.get("WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    parser.add_argument("--api-prefix", default=os.environ.get("API_PREFIX", "/api/v1"))
    parser.add_argument("--chrome", default=os.environ.get("CHROME_PATH"))
    parser.add_argument("--crawler-id", default=os.environ.get("CRAWLER_ID", ""),
                        help="当前爬虫 ID, 用于 get/set_login_ticket 关联 MongoDB 中的登录凭据")
    parser.add_argument("--mongo-uri", default=os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27017"),
                        help="MongoDB 连接地址")
    parser.add_argument("--mongo-db", default=os.environ.get("MONGO_DB", "crawler"),
                        help="MongoDB 数据库名")
    parser.add_argument("--llm-provider", default=os.environ.get("LLM_PROVIDER", "deepseek"),
                        help="LLM 服务商: deepseek / dashscope / openai / 其他 OpenAI 兼容接口")
    parser.add_argument("--llm-model", default=os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
                        help="LLM 模型名, 如 deepseek-chat / qwen-plus / gpt-4o")
    parser.add_argument("--llm-api-key", default=os.environ.get("LLM_API_KEY", ""),
                        help="LLM API Key (爬虫 Agent 必需)")
    parser.add_argument("--llm-base-url", default=os.environ.get("LLM_BASE_URL", ""),
                        help="LLM 兼容接口 base URL, 留空按 provider 自动推断")
    parser.add_argument("--llm-temperature", type=float,
                        default=float(os.environ.get("LLM_TEMPERATURE", "0.2")),
                        help="LLM 采样温度")
    parser.add_argument("--dev-limit", dest="dev_limit",
                        action=argparse.BooleanOptionalAction,
                        default=os.environ.get("DEV_LIMIT", "1") != "0",
                        help="开发测试模式限制爬取数据量(默认开启); 同步上线时加 --no-dev-limit 关闭")
    parser.add_argument("--max-items", type=int,
                        default=int(os.environ.get("MAX_ITEMS", "50")),
                        help="开发模式 save_content / limit_items 对列表/迭代的最大条数")
    parser.add_argument("--max-bytes", type=int,
                        default=int(os.environ.get("MAX_BYTES", str(512 * 1024))),
                        help="开发模式单次保存(save_content 文本 / save_page HTML)的最大字节数")
    args = parser.parse_args()
    api_prefix = args.api_prefix.strip()
    if not api_prefix.startswith("/"):
        api_prefix = "/" + api_prefix
    api_prefix = api_prefix.rstrip("/")
    return Config(
        display=args.display,
        width=args.width,
        height=args.height,
        framerate=args.framerate,
        jpeg_quality=args.quality,
        cdp_port=args.cdp_port,
        web_host=args.host,
        web_port=args.port,
        api_prefix=api_prefix,
        chrome=args.chrome,
        crawler_id=args.crawler_id,
        mongo_uri=args.mongo_uri,
        mongo_db=args.mongo_db,
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        llm_api_key=args.llm_api_key,
        llm_base_url=args.llm_base_url,
        llm_temperature=args.llm_temperature,
        dev_limit=args.dev_limit,
        max_items=args.max_items,
        max_bytes=args.max_bytes,
    )
