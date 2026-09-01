"""Schema 层: HTTP 接口的请求/响应约束(Pydantic 模型)"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class NavigateRequest(BaseModel):
    url: str = Field(description="要导航到的 URL")
    new_page: bool = False


class NavigateResult(BaseModel):
    url: str
    title: str


class RunRequest(BaseModel):
    code: str = Field(description="要执行的 Playwright 代码")
    run_id: str | None = Field(default=None, description="前端生成的运行标识, 用于关联登录请求轮询")


class SavedItem(BaseModel):
    id: str
    kind: str
    name: str
    path: str
    size: int
    content: str


class RunResult(BaseModel):
    ok: bool
    output: str
    error: str
    saved: list[SavedItem] = []


class RunLoginAnswerRequest(BaseModel):
    answers: dict = Field(description="登录表单答案: 账号/密码/验证码, 或 {cancelled: true}")


class RunLoginActionRequest(BaseModel):
    action: str = Field(description="登录动作: send_code / refresh_captcha / refresh_qr")


class RunLoginCaptcha(BaseModel):
    type: str = "none"
    input_key: str | None = None
    input_selector: str | None = None
    send_selector: str | None = None
    image_selector: str | None = None
    image: str | None = None


class RunLoginField(BaseModel):
    key: str
    label: str
    input_type: str = "text"
    placeholder: str | None = None


class RunLoginRequest(BaseModel):
    qid: str = ""
    login_type: str = "account"
    method: str | None = None
    url: str | None = None
    zoom_browser: bool | None = None
    message: str | None = None
    timeout: float | None = None
    fields: list[RunLoginField] = []
    captcha: RunLoginCaptcha = RunLoginCaptcha()
    submit_label: str = "登录"


class RunLoginResult(BaseModel):
    run_id: str
    waiting: bool
    request: RunLoginRequest | None = None


class RunLoginAnswerResult(BaseModel):
    ok: bool
    message: str | None = None


class FormatRequest(BaseModel):
    code: str = Field(description="要格式化的 Python 代码")


class FormatResult(BaseModel):
    ok: bool
    formatted: str
    error: str


class PageInfo(BaseModel):
    id: str
    url: str
    title: str


class CaptureStatus(BaseModel):
    running: bool
    error: str | None
    viewers: int
    fps: float | None
    frames_total: int
    last_frame_age_ms: float | None


class ConsoleStatus(BaseModel):
    targets: int
    connections: int
    subscribers: dict[str, int]
    history: int


class EvalRequest(BaseModel):
    expression: str = Field(description="要在浏览器活动页面中执行的 JS 表达式")


class EvalItem(BaseModel):
    k: str
    t: str | None = None
    v: str | None = None
    oid: str | None = None
    sub: str | None = None
    cls: str | None = None
    prev: list[dict] | None = None
    style: str | None = None


class EvalResult(BaseModel):
    ok: bool
    item: EvalItem | None = None
    error: str | None = None
    stack: str | None = None


class PropertiesRequest(BaseModel):
    object_id: str = Field(description="要展开对象的 CDP objectId")


class PropertyEntry(BaseModel):
    name: str
    item: EvalItem


class PropertiesResult(BaseModel):
    ok: bool
    props: list[PropertyEntry] = []
    error: str | None = None


class NetworkBodyRequest(BaseModel):
    request_id: str = Field(description="Network 请求 ID")


class DomBoxRequest(BaseModel):
    backend_node_id: int = Field(description="DOM 节点的 backendNodeId")


class StorageItemsRequest(BaseModel):
    origin: str = Field(description="页面 origin")
    session: bool = False


class StorageSetRequest(BaseModel):
    origin: str
    session: bool = False
    key: str
    value: str = ""


class CookieSetRequest(BaseModel):
    origin: str
    name: str
    value: str = ""
    path: str = "/"
    domain: str | None = None
    http_only: bool = False
    secure: bool = False


class CookieDeleteRequest(BaseModel):
    origin: str
    name: str


class IdbStoresRequest(BaseModel):
    origin: str
    database: str


class IdbDataRequest(BaseModel):
    origin: str
    database: str
    store: str
    skip: int = 0
    count: int = 50


class StatusResult(BaseModel):
    uptime: float | None
    error: str | None
    xvfb: bool
    chrome: bool
    chrome_cdp: str
    capture: CaptureStatus
    cdp: ConsoleStatus | None
    pages: list[PageInfo]


class CodeCommitRequest(BaseModel):
    """把工作区内容固化为一次提交。message 必填, 非空且 ≤ 200 字符。"""

    message: str = Field(..., max_length=200, description="提交信息, 非空且 ≤ 200 字符")
    content: str = Field(..., description="工作区完整源码")
    author: str = Field(default="unknown", max_length=64, description="提交人名称")
    crawler_id: str | None = Field(default=None, description="按 crawler 隔离, 缺省回退 default")

    @field_validator("message")
    @classmethod
    def _message_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("提交信息不能为空")
        return v


class CodeCheckoutRequest(BaseModel):
    """把指定提交的内容检出到工作区(不动 HEAD)。"""

    commit_id: str = Field(..., description="要检出的提交")
    crawler_id: str | None = Field(default=None, description="按 crawler 隔离, 缺省回退 default")


class CodeHeadInfo(BaseModel):
    commit_id: str
    message: str
    created_at: int


class CodeRepoResult(BaseModel):
    crawler_id: str
    has_commits: bool
    head: CodeHeadInfo | None = None


class CodeCommitInfo(BaseModel):
    commit_id: str
    parent: str | None = None
    message: str
    author: str = "unknown"
    created_at: int
    size: int
    content: str
    content_hash: str


class CodeCommitResult(BaseModel):
    ok: bool
    commit: CodeCommitInfo


class CodeCommitSummary(BaseModel):
    commit_id: str
    parent: str | None = None
    message: str
    author: str = "unknown"
    created_at: int
    size: int
    stat: dict[str, int] = Field(default_factory=lambda: {"add": 0, "del": 0})


class CodeCommitListResult(BaseModel):
    commits: list[CodeCommitSummary] = []


class CodeCheckoutResult(BaseModel):
    ok: bool
    commit_id: str
    code: str
