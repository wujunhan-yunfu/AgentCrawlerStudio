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
    web_prefix: str = "/"
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
    verify_enabled: bool = True            # 人机验证处理总开关
    verify_max_attempts: int = 3           # 单次验证触发的自动尝试上限
    verify_slider_only_right: bool = True  # 无缺口纯滑块优先拖到最右
    verify_vision_provider: str = ""       # 多模态模型 provider(类型判定/点选共用)
    verify_vision_model: str = ""          # 看图模型名(可空)
    verify_vision_base_url: str = ""       # 多模态模型兼容网关
    verify_rule_fallback: bool = True      # 无视觉模型时允许规则强命中兜底
    verify_runtime_exit: bool = True       # 产物运行期超限即结束本次运行

    # ---- 运行模式(CDC 编排用) ----
    mode: str = "dev"                      # dev / login / run
    run_type: str = "once"                 # run 模式: once / cron
    cron: str = ""                         # run 模式 cron 表达式
    run_id: str = ""                       # 本次运行 ID(CDC 生成, 回传 webhook)
    webhook_url: str = ""                  # 运行事件 webhook 回调地址
    webhook_secret: str = ""               # webhook HMAC 签名密钥
    heartbeat_url: str = ""                # 定时任务心跳地址(缺省由 webhook_url 推导)
    heartbeat_interval: int = 30           # 心跳间隔(秒), 仅 run+cron 生效
    data_webhook_url: str = ""             # 爬取数据 webhook 地址(缺省由 webhook_url 推导)
    data_inline_max_bytes: int = 1024 * 1024  # 数据内联阈值(超过走预签名直传, 预留)
    data_webhook_sync: bool = False        # save_content 是否同步等待 CDC 确认
    login_timeout: float = 300.0           # login 模式总超时(秒)
    source: str = "editor"                 # 代码来源: editor / mongo(login/run 固定 mongo)
    headless: bool | None = None           # 无头模式(None 按模式默认: run 无头)
    serve: bool = False                    # login 模式暴露实时画面服务(CDC 场景固定开启)


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


def _norm_prefix(raw: str, *, default: str = "/") -> str:
    value = raw.strip()
    if not value:
        return default
    if not value.startswith("/"):
        value = "/" + value
    value = value.rstrip("/")
    return value or default


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
    parser.add_argument("--web-prefix", default=os.environ.get("WEB_PREFIX", "/"),
                        help="网页控制台与静态资源访问前缀(默认 /); API/WS 前缀由 --api-prefix 控制")
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
    parser.add_argument("--verify-enabled", dest="verify_enabled",
                        action=argparse.BooleanOptionalAction,
                        default=os.environ.get("VERIFY_ENABLED", "1") != "0",
                        help="人机验证处理总开关(默认开启)")
    parser.add_argument("--verify-max-attempts", type=int,
                        default=int(os.environ.get("VERIFY_MAX_ATTEMPTS", "3")),
                        help="单次验证触发的自动尝试上限")
    parser.add_argument("--verify-slider-only-right", dest="verify_slider_only_right",
                        action=argparse.BooleanOptionalAction,
                        default=os.environ.get("VERIFY_SLIDER_ONLY_RIGHT", "1") != "0",
                        help="无缺口纯滑块优先拖到最右")
    parser.add_argument("--verify-vision-provider", default=os.environ.get("VERIFY_VISION_PROVIDER", ""),
                        help="多模态模型 provider(类型判定/点选共用, 可空)")
    parser.add_argument("--verify-vision-model", default=os.environ.get("VERIFY_VISION_MODEL", ""),
                        help="看图模型名(可空; 空则按规则/主模型判定)")
    parser.add_argument("--verify-vision-base-url", default=os.environ.get("VERIFY_VISION_BASE_URL", ""),
                        help="多模态模型兼容网关 base URL")
    parser.add_argument("--verify-rule-fallback", dest="verify_rule_fallback",
                        action=argparse.BooleanOptionalAction,
                        default=os.environ.get("VERIFY_RULE_FALLBACK", "1") != "0",
                        help="无视觉模型时允许规则强命中兜底判定")
    parser.add_argument("--no-verify-runtime-exit", dest="verify_runtime_exit",
                        action="store_false",
                        default=os.environ.get("VERIFY_RUNTIME_EXIT", "1") != "0",
                        help="产物运行期超限不结束本次运行(调试用, 仍不弹窗)")
    # ---- 运行模式(CDC 编排用) ----
    parser.add_argument("--mode", default=os.environ.get("MODE", "dev"),
                        help="运行模式: dev(默认) / login / run")
    parser.add_argument("--run-type", default=os.environ.get("RUN_TYPE", "once"),
                        help="run 模式: once 一次性 / cron 定时")
    parser.add_argument("--cron", default=os.environ.get("CRON", ""),
                        help="run 模式 cron 表达式")
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID", ""),
                        help="本次运行 ID(CDC 生成, 回传 webhook)")
    parser.add_argument("--webhook-url", default=os.environ.get("WEBHOOK_URL", ""),
                        help="运行事件 webhook 回调地址")
    parser.add_argument("--webhook-secret", default=os.environ.get("WEBHOOK_SECRET", ""),
                        help="webhook HMAC 签名密钥")
    parser.add_argument("--heartbeat-url", default=os.environ.get("HEARTBEAT_URL", ""),
                        help="定时任务心跳地址(缺省由 webhook-url 推导)")
    parser.add_argument("--heartbeat-interval", type=int,
                        default=int(os.environ.get("HEARTBEAT_INTERVAL", "30")),
                        help="心跳间隔(秒), 仅 run+cron 生效")
    parser.add_argument("--data-webhook-url", default=os.environ.get("DATA_WEBHOOK_URL", ""),
                        help="爬取数据 webhook 地址(缺省由 webhook-url 推导)")
    parser.add_argument("--data-inline-max-bytes", type=int,
                        default=int(os.environ.get("DATA_INLINE_MAX_BYTES", str(1024 * 1024))),
                        help="数据内联阈值(字节), 超过走预签名直传(预留)")
    parser.add_argument("--data-webhook-sync", dest="data_webhook_sync",
                        action=argparse.BooleanOptionalAction,
                        default=os.environ.get("DATA_WEBHOOK_SYNC", "0") != "0",
                        help="save_content 是否同步等待 CDC 确认(默认异步)")
    parser.add_argument("--login-timeout", type=float,
                        default=float(os.environ.get("LOGIN_TIMEOUT", "300")),
                        help="login 模式总超时(秒)")
    parser.add_argument("--source", default=os.environ.get("SOURCE", "editor"),
                        help="代码来源: editor / mongo(login/run 固定 mongo)")
    parser.add_argument("--headless", dest="headless",
                        action=argparse.BooleanOptionalAction,
                        default=None if "HEADLESS" not in os.environ
                        else os.environ.get("HEADLESS", "") != "0",
                        help="无头模式运行(默认按模式: run 无头)")
    parser.add_argument("--serve", dest="serve",
                        action=argparse.BooleanOptionalAction,
                        default=os.environ.get("SERVE", "0") != "0",
                        help="login 模式暴露实时画面服务(CDC 场景固定开启)")
    args = parser.parse_args()
    api_prefix = _norm_prefix(args.api_prefix, default="")
    web_prefix = _norm_prefix(args.web_prefix, default="/")
    return Config(
        display=args.display,
        width=args.width,
        height=args.height,
        framerate=args.framerate,
        jpeg_quality=args.quality,
        cdp_port=args.cdp_port,
        web_host=args.host,
        web_port=args.port,
        web_prefix=web_prefix,
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
        verify_enabled=args.verify_enabled,
        verify_max_attempts=args.verify_max_attempts,
        verify_slider_only_right=args.verify_slider_only_right,
        verify_vision_provider=args.verify_vision_provider,
        verify_vision_model=args.verify_vision_model,
        verify_vision_base_url=args.verify_vision_base_url,
        verify_rule_fallback=args.verify_rule_fallback,
        verify_runtime_exit=args.verify_runtime_exit,
        mode=args.mode,
        run_type=args.run_type,
        cron=args.cron,
        run_id=args.run_id,
        webhook_url=args.webhook_url,
        webhook_secret=args.webhook_secret,
        heartbeat_url=args.heartbeat_url,
        heartbeat_interval=args.heartbeat_interval,
        data_webhook_url=args.data_webhook_url,
        data_inline_max_bytes=args.data_inline_max_bytes,
        data_webhook_sync=args.data_webhook_sync,
        login_timeout=args.login_timeout,
        source=args.source,
        headless=args.headless,
        serve=args.serve,
    )
