# 爬虫数据中心项目规划（基于 AgentCrawlerStudio）

> 本文档规划一个以 **AgentCrawlerStudio（本项目，下称 ACS）** 为基座新建的「爬虫数据中心」（Crawler Data Center，下称 CDC），
> 以及为支撑 CDC 对本项目所需做的改造（新增 `login` / `run` 两种运行模式 + webhook 回调）。

---

## 1. 概述

### 1.1 背景

ACS 目前是一个「单实例、面向开发者」的 Playwright 开发调试平台：Xvfb + 有头 Chrome + 抓屏 + WebSocket 实时画面，
前端提供 Monaco 编辑器、Agent 会话、代码版本、导出脚本等能力。所有隔离维度只有一个 `crawler_id`
（见 `backend/config.py:94`、`backend/services/crawler.py:641`）。

CDC 需要在其之上提供**多租户、多工作空间、多爬虫项目**的管理与调度能力，并复用 ACS 作为
「自定义代码」的**开发容器**与**运行容器**。

### 1.2 目标

1. 新建 CDC 控制面项目：用户系统、RBAC、工作空间、爬虫项目、调度与运行记录、数据落库。
2. 爬虫项目支持两种来源：**项目内置爬取器**（CDC 自带）与**自定义代码**（复用 ACS）。
3. 自定义代码流程：前端进入 loading 页 → 后台通过 K8s 以 `crawler_id` 拉起属于该项目的 Pod →
   就绪后解除 loading 并进入开发容器（即 ACS）。
4. 为 ACS 新增两种运行模式：
   - **login 模式**：不可编辑代码，进入后自动运行 MongoDB 中的代码，仅运行到「登录 + 登录保存」，
     成功登录或获取到新凭据并保存后即结束。
   - **run 模式**：按传入参数定时运行或一次性运行，并在开始 / 结束 / 失败时回调 webhook；
     登录失败视为一种运行失败。需设计 webhook 接口的报文与返回。

### 1.3 范围

| 范围 | 内容 |
| --- | --- |
| **新项目（CDC）** | 控制面 API、前端、数据模型、K8s 编排、调度、webhook 接收、数据存储 |
| **本项目改造（ACS）** | 运行模式（dev/login/run）、配置、入口分发、代码来源、健康探针、webhook 发送、login 结束判定（复用既有登录链路） |
| **不在本次范围** | 内置爬取器的具体业务实现、数据可视化大屏、计费 |

### 1.4 名词表

| 名词 | 含义 |
| --- | --- |
| CDC | 爬虫数据中心，新建的控制面项目 |
| ACS | AgentCrawlerStudio，本项目，作为开发容器与运行容器 |
| Workspace | 工作空间，资源与权限边界 |
| Crawler Project | 工作空间下的爬虫项目，来源为内置或自定义 |
| crawler_id | ACS 的隔离标识，CDC 中与「爬虫项目」一一对应，作为 Pod 参数下发 |
| Dev Container | 自定义代码的开发容器，即运行 `MODE=dev` 的 ACS Pod |
| login mode | ACS 新增模式：只做登录与凭据保存 |
| run mode | ACS 新增模式：一次性 / 定时运行 + webhook 回调 |

---

## 2. 总体架构

### 2.1 分层

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          浏览器（CDC 前端 SPA）                             │
│   用户/登录  |  工作空间  |  爬虫项目列表  |  loading 页  |  运行记录/数据     │
└───────────────┬──────────────────────────────────────────────────────────┘
                │ HTTPS (CDC API, JWT)
┌───────────────▼──────────────────────────────────────────────────────────┐
│                         CDC 控制面（新项目）                                │
│  Auth/RBAC | Workspace | CrawlerProject | DevContainer Orchestrator       │
│  Scheduler (cron/once) | Webhook Receiver | Run/Result Store | Audit      │
│  MongoDB                                                                  │
└───────┬───────────────────────────┬───────────────────────┬──────────────┘
        │ K8s API                   │ 反向代理 / Ingress     │ webhook 出站
        ▼                           ▼                       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         K8s 集群（数据面）                                  │
│  ACS Pod(MODE=dev)   ── Service ── Ingress ──► 用户浏览器访问开发容器        │
│  ACS Pod(MODE=login) ── 一次性，登录+保存凭据后退出                          │
│  ACS Pod(MODE=run)   ── 一次性/定时，运行爬取并回调 CDC webhook              │
│  MongoDB（共享：login_tickets / code_commits / code_repos / agent_* ）     │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.2 控制面 / 数据面职责

| 组件 | 职责 |
| --- | --- |
| CDC 控制面 | 用户与权限、项目元数据、拉起/回收 Pod、调度、webhook 接收、运行记录与结果落库 |
| ACS Pod | 承载真实浏览器与代码执行；按 `MODE` 决定暴露 UI 还是执行任务 |
| MongoDB | 元数据（CDC）+ 代码版本 / 登录凭据 / 会话（ACS）共享库，按 `crawler_id` 隔离 |
| K8s | Pod 生命周期、Service/Ingress、资源限制、就绪探针 |

### 2.3 关键隔离原则

- **crawler_id 即项目隔离键**：CDC 的 `crawler_project.crawler_id` 与 ACS 的 `--crawler-id` 完全一致。
- 代码（`code_commits` / `code_repos`）、登录凭据（`login_tickets`）、Agent 会话（`agent_sessions`）
  均已按 `crawler_id` 隔离（见 `backend/services/code_version.py:7`、`backend/services/crawler.py:624`），可直接复用。
- 一个爬虫项目在同一时刻**最多一个 dev Pod**（可 scale-to-zero），login/run 为短生命周期 Pod。

---

## 3. CDC 数据中心设计

### 3.1 用户系统

- 注册 / 登录 / 登出，密码使用 `bcrypt`/`argon2` 哈希。
- 认证方式：JWT（Access Token + Refresh Token），或对接企业 SSO（OIDC）。
- 用户状态：`active` / `disabled` / `pending`。
- 可选：邮箱验证、密码重置、登录审计。

### 3.2 RBAC

采用「角色 → 权限」+「用户 → 角色绑定（可限定 scope）」模型。scope 支持
`global` / `workspace` / `project` 三级。

**内置角色**

| 角色 | scope | 说明 |
| --- | --- | --- |
| `super_admin` | global | 平台管理员，全部权限 |
| `workspace_owner` | workspace | 工作空间所有者，管理成员与全部项目 |
| `workspace_admin` | workspace | 工作空间管理员，管理项目与成员（不含删除空间） |
| `developer` | workspace/project | 创建/编辑代码、提交版本、触发 login 运行 |
| `operator` | workspace/project | 触发 run、查看运行记录与数据，不可改代码 |
| `viewer` | workspace/project | 只读 |

**权限点（示例）**

| 权限 | 说明 |
| --- | --- |
| `workspace:create/read/update/delete` | 工作空间 CRUD |
| `member:invite/remove/update_role` | 成员管理 |
| `project:create/read/update/delete` | 爬虫项目 CRUD |
| `code:read/edit/commit/checkout` | 自定义代码操作 |
| `credential:read/write` | 登录凭据查看/写入 |
| `run:trigger` | 触发 login / run |
| `run:read` | 查看运行记录 |
| `data:read` | 查看抓取结果 |

**实现建议**：策略表存 Mongo（`roles` / `permissions` / `role_bindings`），
鉴权中间件按 `(user, permission, scope)` 校验；规模扩大后可切换 Casbin。

### 3.3 工作空间

- 资源与权限边界，包含成员、爬虫项目、共享凭据配额、数据存储配额。
- 创建者自动成为 `workspace_owner`。
- 删除空间需二次确认，级联处理项目与容器回收（软删除优先）。

### 3.4 爬虫项目

| 字段 | 说明 |
| --- | --- |
| `project_id` | 项目 ID |
| `workspace_id` | 所属工作空间 |
| `name` / `description` | 名称/描述 |
| `source_type` | `builtin`（内置爬取器）/ `custom`（自定义代码） |
| `builtin_crawler_id` | source_type=builtin 时指向内置爬取器 |
| `crawler_id` | source_type=custom 时与 ACS 隔离键一致（唯一） |
| `dev_container` | 开发容器状态引用（见 4.4） |
| `run_config` | 默认运行方式（once/cron 表达式/headless/webhook 等） |
| `status` | `active` / `disabled` |

**项目来源选择**

- `builtin`：由 CDC 直接按内置爬取器配置执行，不拉起 ACS（本次仅预留接口与模型）。
- `custom`：进入 4 的 K8s 开发容器流程，代码由 ACS 管理。

### 3.5 数据模型（MongoDB 集合）

| 集合 | 关键字段 | 说明 |
| --- | --- | --- |
| `users` | user_id, username, email, password_hash, status | 用户 |
| `roles` | role_id, name, scope, permissions[] | 角色 |
| `role_bindings` | user_id, role_id, workspace_id, project_id | 用户角色绑定 |
| `workspaces` | workspace_id, name, owner_id, quota, created_at | 工作空间 |
| `crawler_projects` | project_id, workspace_id, source_type, crawler_id, run_config | 爬虫项目 |
| `dev_containers` | crawler_id, pod_name, service_name, dev_url, phase, last_active_at | 开发容器状态 |
| `runs` | run_id, project_id, crawler_id, mode, trigger, status, started_at, finished_at, error | 运行记录 |
| `run_processes` | crawler_id, process_id, run_type, pod_name, cron, status, last_heartbeat_at, uptime_ms, health, next_run_at, last_run | **定时任务进程心跳与存活状态**（见 7.11） |
| `run_results` | run_id, saved[]（文件名/类型/大小/存储路径） | 抓取结果索引 |
| `data_items` | save_id, run_id, crawler_id, seq, kind, fmt, name, size, content_hash, storage_path, source_url, collected_at | **每次 `save_content` 推送的数据索引**（见 7.12） |
| `webhook_deliveries` | delivery_id, run_id, url, event, attempt, status_code, response, next_retry_at | webhook 投递记录 |
| `audit_logs` | actor_id, action, target, ts, ip | 审计 |
| `login_tickets` | crawler_id, host, ticket, updated_at | **复用 ACS 既有集合** |
| `code_commits` / `code_repos` | crawler_id, ... | **复用 ACS 既有集合** |

> 所有集合加 `workspace_id`（冗余）便于按空间过滤与配额统计。

### 3.6 CDC API 概览（前缀 `/api/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/login` `/auth/refresh` `/auth/logout` | 认证 |
| GET/POST | `/workspaces` | 工作空间 |
| POST | `/workspaces/{id}/members` | 成员与角色绑定 |
| GET/POST | `/workspaces/{id}/projects` | 项目列表/创建 |
| GET/PATCH/DELETE | `/projects/{id}` | 项目详情 |
| POST | `/projects/{id}/dev-container` | 确保开发容器拉起（返回状态/URL） |
| GET | `/projects/{id}/dev-container` | 轮询容器状态 |
| POST | `/projects/{id}/login` | 触发 login 模式运行（返回 `run_id` 与只读实时画面入口） |
| GET | `/projects/{id}/login/live` | **WebSocket 反向代理**：内嵌 login 实时画面（只读，短时效令牌鉴权） |
| POST | `/projects/{id}/runs` | 触发 run（once/cron） |
| GET | `/projects/{id}/runs` | 运行记录 |
| GET | `/runs/{run_id}` | 运行详情/结果 |
| GET | `/projects/{id}/run-process` | 查询定时任务进程的存活/健康状态（见 7.11） |
| POST | `/webhooks/runs` | **ACS → CDC 的运行回调接收端点** |
| POST | `/webhooks/heartbeat` | **ACS → CDC 的定时任务心跳端点**（存活探活，见 7.11） |
| POST | `/webhooks/data` | **ACS → CDC 的爬取数据推送端点**（`save_content` 逐条推送，见 7.12） |
| POST | `/webhooks/data/prepare` `/webhooks/data/commit` | 大文件预签名直传：申请上传地址 / 提交确认（见 7.12） |

---

## 4. 开发容器编排（K8s）

### 4.1 触发流程（loading 页）

```
用户点击「自定义代码」
   │
   ▼
CDC 前端跳转 /projects/{id}/loading
   │  POST /projects/{id}/dev-container
   ▼
CDC 控制面
   │  1) 校验权限 + 项目为 custom + crawler_id 存在
   │  2) 查 dev_containers：ready 直接返回 dev_url
   │  3) 否则创建/复用 Pod（env: CRAWLER_ID, MODE=dev, MONGO_URI, LLM_*）
   ▼
K8s 拉起 ACS Pod → 启动 Xvfb + Chrome + 抓屏 + FastAPI
   │  就绪探针 GET /healthz 通过
   ▼
CDC 前端轮询 GET /projects/{id}/dev-container
   │  phase=ready → 返回 dev_url
   ▼
解除 loading → 跳转/内嵌开发容器（ACS UI）
```

### 4.2 Pod / Service / Ingress 规格

**Pod（dev）**

```yaml
containers:
  - name: acs
    image: agentcrawlerstudio:<version>
    env:
      - { name: MODE,        value: "dev" }
      - { name: CRAWLER_ID,  value: "<project.crawler_id>" }
      - { name: MONGO_URI,   value: "<shared mongo>" }
      - { name: MONGO_DB,    value: "crawler" }
      - { name: LLM_PROVIDER, value: "deepseek" }
      - { name: LLM_MODEL,   value: "deepseek-v4-flash" }
      - { name: LLM_API_KEY, valueFrom: { secretKeyRef: {...} } }
      - { name: WEB_PORT,    value: "8080" }
      - { name: API_PREFIX,  value: "/api/v1" }
    resources:
      limits:   { cpu: "2",  memory: "2Gi" }
      requests: { cpu: "500m", memory: "512Mi" }
    readinessProbe:
      httpGet: { path: /healthz, port: 8080 }
      initialDelaySeconds: 5
      periodSeconds: 3
```

**Service / Ingress**

- 每项目一个 `ClusterIP` Service。
- Ingress 路由建议用子域：`https://<crawler_id>.dev.cdc.internal/`，或路径：
  `/dev/{workspace_id}/{project_id}/*`（需 ACS 支持 `--web-prefix`，已具备，见 `backend/config.py:90`）。
- 由 CDC 反向代理统一鉴权后再转发（推荐，便于审计与统一域名）。

### 4.3 crawler_id 传递与隔离

- Pod env `CRAWLER_ID` = 项目 `crawler_id`；ACS 启动参数 `--crawler-id`。
- 该值贯穿：登录凭据（`get_login_ticket` / `set_login_ticket`）、代码版本（`code_commits`）、
  Agent 会话（`agent_sessions`），实现项目间完全隔离。
- 禁止前端传入任意 `crawler_id`，一律由 CDC 控制面按项目推导后下发。

### 4.4 状态机与就绪探针

`dev_containers.phase`：

```
absent → pending(创建中) → starting(容器启动) → ready(探针通过)
                 │                 │
                 └──── failed ◄────┘（超时/镜像拉取失败/探针失败）
ready → stopping → absent（空闲回收/TTL）
```

- ACS 需新增 `GET /healthz`（见 5.5），返回进程、Xvfb、Chrome、CDP 是否就绪。
- CDC 侧对 `pending/starting` 设总超时（如 120s），超时置 `failed` 并返回可读错误。

### 4.5 前端 loading 页

- 展示阶段文案：`申请容器 → 启动浏览器环境 → 等待就绪`，轮询间隔 1–2s，最长 120s。
- 失败态给出重试按钮与错误码（`IMAGE_PULL_FAILED` / `START_TIMEOUT` / `QUOTA_EXCEEDED`）。
- 就绪后跳转 `dev_url`；可选支持 iframe 内嵌（需 ACS 允许被嵌入，注意 `X-Frame-Options`）。

### 4.6 生命周期 / 回收

- 空闲 TTL（如 30 分钟无操作）自动 scale-to-zero。
- 每工作空间并发容器配额，超限拒绝并提示。
- 项目删除 / 空间删除时级联删除 Pod/Service/Ingress。

---

## 5. 本项目（ACS）改造规划

### 5.1 运行模式总览

新增 `--mode`（env `MODE`），取值：

| 模式 | 用途 | UI | 编辑器 | Agent | 退出行为 |
| --- | --- | --- | --- | --- | --- |
| `dev` | 默认，当前能力 | 完整 UI | ✅ | ✅ | 常驻 |
| `login` | 登录与凭据保存 | 极简 + 只读实时画面 | ❌ | ❌ | 完成后退出 |
| `run` | 定时/一次性运行 | 无（CLI/日志） | ❌ | ❌ | 完成/调度结束 |

### 5.2 新增配置项（`backend/config.py`）

| 参数 | env | 默认 | 说明 |
| --- | --- | --- | --- |
| `--mode` | `MODE` | `dev` | `dev` / `login` / `run` |
| `--run-type` | `RUN_TYPE` | `once` | run 模式：`once` / `cron` |
| `--cron` | `CRON` | `""` | run 模式定时表达式（复用 `backend/services/cron.py` 校验） |
| `--run-id` | `RUN_ID` | `""` | 本次运行 ID（CDC 生成，回传 webhook） |
| `--webhook-url` | `WEBHOOK_URL` | `""` | 运行事件回调地址 |
| `--webhook-secret` | `WEBHOOK_SECRET` | `""` | HMAC 签名密钥 |
| `--heartbeat-url` | `HEARTBEAT_URL` | 由 webhook 推导 | 定时任务心跳地址（默认 `WEBHOOK_URL` 同源 `/webhooks/heartbeat`） |
| `--heartbeat-interval` | `HEARTBEAT_INTERVAL` | `30` | 心跳间隔（秒），仅 `run` + `cron` 生效 |
| `--data-webhook-url` | `DATA_WEBHOOK_URL` | 由 webhook 推导 | 数据推送地址（默认同源 `/webhooks/data`）；为空则关闭推送 |
| `--data-inline-max-bytes` | `DATA_INLINE_MAX_BYTES` | `1048576` | 数据内联阈值，超过走预签名直传 |
| `--data-webhook-sync` | `DATA_WEBHOOK_SYNC` | `0` | `1`=`save_content` 同步等待确认；`0`=异步队列 |
| `--login-timeout` | `LOGIN_TIMEOUT` | `300` | login 模式总超时（秒） |
| `--source` | `SOURCE` | `editor` | 代码来源：`editor` / `mongo`（login/run 固定 `mongo`） |
| `--headless` | `HEADLESS` | 按模式 | login 默认可有头（便于扫码）；run 默认无头 |
| `--serve` | `SERVE` | login 固定开启 | login 模式仅暴露实时画面服务（只读），供 CDC 内嵌（CDC 场景必须为 1） |

### 5.3 模式路由与入口分发（`backend/main.py`）

- 将 `create_app()` 改为按 `cfg.mode` 决定挂载的路由：
  - `dev`：全部路由（现状不变）。
  - `login`：仅挂 `status` + 实时画面流（`--serve`，CDC 场景固定开启）+ 运行结果查询；**不挂** agent/versions/export/input 及任何登录交互接口。
  - `run`：不启动 Web 服务，直接进入 `run_mode` 执行器。
- `main()` 分发：
  ```python
  if cfg.mode == "run":
      return run_mode.main(cfg)          # CLI/调度
  app = create_app(cfg)                  # dev / login 启动 Web
  uvicorn.run(...)
  ```
- login/run 完成后以退出码反映结果（0 成功，非 0 失败），供 K8s Job/Pod 判定。

### 5.4 代码来源（Mongo HEAD）

- 新增 `backend/services/runmode/code_source.py`：
  - 复用 `backend/services/code_version.py` 的 `CodeStore`，读取 `crawler_id` 的 HEAD commit content。
  - 无提交时返回明确错误（login/run 均失败）。
- 与 `--source mongo` 对应，避免依赖浏览器端未提交内容。

### 5.5 健康 / 就绪接口

- 新增 `GET /healthz`（不走 `/api/v1` 前缀，供探针使用）：
  ```json
  { "ok": true, "mode": "dev", "xvfb": true, "chrome": true, "cdp": true, "crawler_id": "..." }
  ```
- 仅当 Xvfb/Chrome/CDP 就绪才返回 200，供 4.4 探针判定。

### 5.6 后端模块改造清单

| 模块 | 改造 |
| --- | --- |
| `backend/config.py` | 新增 5.2 配置项与 `mode` 字段 |
| `backend/main.py` | 按 mode 分发入口、按 mode 挂载路由、注册 `/healthz` |
| `backend/routers/*` | 模式门禁（login 下禁用编辑类接口） |
| `backend/services/crawler.py` | **不改登录组件**；`CrawlerEnv` 的 `page_login` / `set_login_ticket` / `capture_login_state` 原样复用 |
| `backend/services/agent/run_login.py` | **不改**；login 模式复用既有 `RunLoginManager` / `StandaloneLoginGate` |
| `backend/services/browser.py` | `run_code` 支持 `source=mongo`（从 Mongo HEAD 取码） |
| **新增** `backend/services/runmode/` | `code_source.py` / `login_mode.py` / `run_mode.py` / `webhook.py` / `heartbeat.py` / `data_webhook.py` / `run_context.py` |
| `backend/services/cron.py` | 直接复用（已有） |
| `backend/services/exporter.py` | 可选：把 login/run 运行时能力同步到导出包 |

---

## 6. login 模式详细设计

### 6.1 目标与约束

- 进入即自动运行 MongoDB 中该 `crawler_id` 的 HEAD 代码，**不可编辑**。
- 只运行到「登录 + 登录保存」阶段：成功登录 **或** 获取到新凭据并保存后立即结束。
- **完整复用既有登录与凭据保存链路，不新增任何登录组件/接口/注入函数**（详见 6.3）。

### 6.2 执行流程

```
启动(MODE=login)
  → 校验 crawler_id / Mongo 可达
  → 读取 HEAD 代码
  → 启动浏览器（默认有头，可由 --headless 覆盖）
  → 复用既有执行链路运行代码（与 /run 相同：RunLoginManager + page_login + 凭据函数）
  → 既有 page_login 完成 + 既有 set_login_ticket/capture_login_state 保存成功 → 结束(ok)
  → 脚本正常结束但未保存凭据            → 结束(failed: login_not_saved)
  → 超时 --login-timeout                → 结束(failed: login_timeout)
  → 异常/登录失败                        → 结束(failed: login_failed)
  → 退出码 + 结果写入 runs + webhook(login.*)
```

### 6.3 登录完成判定（复用既有流程，不新增功能）

login 模式**完整复用**本项目在开发 / 运行脚本时使用的登录与凭据保存链路，不新增登录组件、接口或注入函数：

- **登录交互**：既有 `page_login`（`backend/services/agent/login.py` 的 `LoginDetector` / `LoginGate`）
  与既有 `/run` 独立运行协作（`backend/services/agent/run_login.py` 的 `RunLoginManager` / `StandaloneLoginGate`）。
- **凭据保存**：既有 `set_login_ticket` / `get_login_ticket`（`login_tickets` 集合，`crawler_id` + `host` 唯一）
  与既有 `capture_login_state` / `restore_login_state`。

结束判定由 **run 编排层**观察既有链路得到，满足以下任一即结束(ok)：

1. `page_login(...)` 返回 `ok=True` 且随后既有 `set_login_ticket(...)` 保存成功；
2. 既有 `set_login_ticket(...)` 直接保存成功（视为脚本已取得新凭据）；
3. 既有 `capture_login_state()` 的结果经既有 `set_login_ticket(...)` 保存成功。

> 该判定仅用于 run 编排层决定「何时结束」，不改变、不扩展既有登录组件的行为；
> 若脚本自然结束且未保存凭据，则按 6.5 记为 `login_not_saved`。

### 6.4 凭据保存

- 复用既有 `set_login_ticket(ticket, host)` → `login_tickets` 集合（`crawler_id` + `host` 唯一）。
- 也支持既有 `capture_login_state()` 返回 cookies/localStorage/sessionStorage 后由脚本
  `set_login_ticket(state, host)` 保存。
- CDC 可提供「凭据已更新」通知（webhook `login.succeeded`）。

### 6.5 失败与超时

| 失败类型 | 触发 | 结果 |
| --- | --- | --- |
| `login_not_saved` | 脚本结束但无凭据写入 | failed |
| `login_timeout` | 超过 `--login-timeout` | failed |
| `login_failed` | `page_login` 返回 ok=False / 抛 `LoginCancelled` | failed |
| `code_error` | 脚本异常 | failed |
| `no_code` | Mongo 无 HEAD | failed |

### 6.6 内嵌实时画面与人工登录（仅使用画面服务）

CDC **必须**在 login 运行期间提供内嵌实时画面入口，且该能力**只使用画面服务（抓屏帧流）**：
CDC 不提供登录表单、不代理登录答案/动作、不注入远程键鼠。用户通过观察实时画面完成登录
（典型为扫描画面中展示的二维码），从而保证：

- **可观测性**：登录全过程（页面跳转、二维码、验证提示）对用户实时可见；
- **用户可登录**：用户用手机扫描画面中的二维码即可完成登录，无需访问容器端口。

两种登录策略并存：

- **自动登录**：`--headless`，脚本用账号/密码/验证码 + `verify_check` 自动完成；
  适合凭据已存在、仅需刷新 token 的场景（此时实时画面仍可打开用于观测）。
- **人工扫码（内嵌画面）**：`--serve --no-headless`，ACS 暴露实时画面；CDC 内嵌该画面，
  脚本负责导航到登录页并确保二维码可见，用户扫码后脚本检测跳转并保存凭据。
  适合首次登录 / 强验证站点。

#### 6.6.1 ACS 侧需暴露的能力（login 模式）

| 能力 | 路径 | 说明 |
| --- | --- | --- |
| 实时画面（唯一对外能力） | `WS /api/v1/stream`（复用 `backend/routers/stream.py`） | 抓屏帧（JPEG）推送，含心跳与断线重连 |

- login 模式**仅**对外暴露实时画面服务；`agent` / `versions` / `export` / `input` 等编辑类接口
  一律**不挂载**。
- 登录本身**完整复用既有 `/run` 独立运行链路**，不新增登录接口或组件：
  内部仍使用既有 `RunLoginManager` / `StandaloneLoginGate` 与既有 `page_login`；
  对外只暴露画面服务，因此不再需要登录答案/动作接口。
- 二维码登录天然适配「只读画面」：既有 `page_login(method="qr")` 的 `_monitor_qr`
  会轮询页面跳转，用户扫画面中的二维码后页面跳转即自动完成，无需前端答复。
- 账号/密码类登录由脚本自动填充（凭据取自既有 `get_login_ticket` 或脚本内置逻辑），
  用户仅通过画面观测；如需人工输入，属于超出本能力的场景，不在本期支持。

> 实时画面与 `/run` 登录协作均已在 ACS 既有代码中实现（`backend/routers/stream.py`、
> `backend/services/browser.py`、`backend/services/agent/run_login.py`），
> login 模式只需在 `create_app` 中挂载画面路由并做模式门禁，无需重写、无需新增功能。

#### 6.6.2 CDC 侧内嵌方案（只代理画面）

- login Pod 创建后暴露 `ClusterIP` Service（或由 CDC 通过 K8s port-forward），**不直接公网暴露**。
- CDC 新增受鉴权的**只读**代理端点：
  - `GET /api/v1/projects/{id}/login/live`（WebSocket Upgrade → 转发到 Pod 的 `/api/v1/stream`）
- 触发 login 的响应携带内嵌入口信息：
  ```json
  {
    "run_id": "run_abc123",
    "status": "running",
    "live_ws_url": "wss://cdc/api/v1/projects/p_123/login/live?token=<short-lived>",
    "expires_at": 1750000300000
  }
  ```
- `token` 为短时效、绑定 `(user, project, run_id)` 的一次性令牌，避免越权观看他人容器画面。
- CDC 前端在项目页以只读 `LiveView` 组件渲染该 WebSocket，**不叠加任何登录表单/操作按钮**，
  仅展示画面与「登录进行中」状态。
- 运行结束（成功/失败/超时）后 CDC 主动关闭画面并回收入口令牌。

#### 6.6.3 安全与体验约束

- 画面仅对具备 `project:read` + `run:trigger`（或专门 `credential:write`）权限的用户开放。
- 代理层做 Origin 校验、限流与审计；禁止将 Pod 端口直接暴露。
- 画面为只读帧流，CDC 不注入任何输入事件。
- 二维码仅在容器内页面渲染，CDC 不落库二维码内容。
- 用户关闭页面不等于取消运行：运行在容器内继续，凭据保存成功即结束（见 6.3）。

### 6.7 状态与退出码

- 结果写入 CDC `runs`（`mode=login`），并回调 webhook（事件见 7.7）。
- 退出码：`0` 成功；`2` 无代码；`3` 登录失败；`4` 超时；`1` 其他错误。

---

## 7. run 模式详细设计

### 7.1 目标与约束

- 按参数执行：**一次性**（once）或**定时**（cron）。
- 开始 / 结束 / 失败均发送 webhook；登录失败归类为运行失败。
- 代码来源为 Mongo HEAD（`--source mongo`）。
- 默认无头运行；`run` 模式不暴露编辑 UI。

### 7.2 触发参数

由 CDC 通过 Pod env/args 下发：

```
MODE=run
CRAWLER_ID=<project.crawler_id>
RUN_ID=<cdc generated>
RUN_TYPE=once|cron
CRON=*/30 * * * *          # RUN_TYPE=cron 时必填
WEBHOOK_URL=https://cdc.internal/api/v1/webhooks/runs
WEBHOOK_SECRET=<hmac key>
HEARTBEAT_INTERVAL=30     # 仅 RUN_TYPE=cron：心跳间隔(秒)
HEADLESS=1
SOURCE=mongo
```

### 7.3 一次性运行（once）

- 启动 → 读代码 → 运行一次 → 上报 → 退出。
- 适合 K8s `Job`（`restartPolicy: Never`），由 CDC 创建并按退出码记录结果。

### 7.4 定时运行（cron）

- 进程常驻，循环：计算下次时间 → sleep → 运行 → 上报 → 继续。
- cron 校验与计算复用 `backend/services/cron.py`（`validate_cron` / `next_runs`）。
- 适合 K8s `Deployment`（replicas=1），CDC 负责启停。
- 每次运行生成独立 `run_id`（可由 `RUN_ID` 加序号，或 ACS 自行生成后上报）。
- 支持并发策略：同一项目默认串行（上一次未结束则跳过并上报 `run.skipped`，可选）。
- 进程常驻，**必须周期性向 CDC 发送心跳**以证明存活与健康（见 7.11）。

### 7.5 运行状态机

```
pending → running → succeeded
                 ├→ failed（含 login_failed / code_error / timeout）
                 └→ skipped（定时并发冲突，可选）
```

### 7.6 webhook 回调

- 触发点：`run.started` / `run.succeeded` / `run.failed`（可选 `run.skipped`）。
- 发送方：ACS `backend/services/runmode/webhook.py`。
- 接收方：CDC `POST /api/v1/webhooks/runs`（3.6）。
- 必须异步、失败重试、不阻塞爬取主流程；发送结果记录到本地日志并随 `run.failed` 上报可观测。

### 7.7 webhook 报文（请求）设计

**Headers**

| Header | 说明 |
| --- | --- |
| `Content-Type` | `application/json` |
| `X-Webhook-Event` | `run.started` / `run.succeeded` / `run.failed` / `run.skipped` |
| `X-Webhook-Delivery` | 投递唯一 ID（uuid），用于幂等 |
| `X-Webhook-Timestamp` | 毫秒时间戳 |
| `X-Webhook-Signature` | `sha256=HMAC_SHA256(secret, timestamp + "." + raw_body)` |

**Body（统一信封）**

```json
{
  "event": "run.failed",
  "delivery_id": "dlv_9f2c...",
  "timestamp": 1750000000000,
  "run": {
    "run_id": "run_abc123",
    "crawler_id": "proj_xxx",
    "project_id": "p_123",
    "workspace_id": "ws_456",
    "mode": "run",
    "trigger": "cron",
    "cron": "*/30 * * * *",
    "run_type": "cron",
    "attempt": 1,
    "status": "failed",
    "started_at": 1749999990000,
    "finished_at": 1750000000000,
    "duration_ms": 10000,
    "error": {
      "type": "login_failed",
      "message": "登录失败: 账号或密码错误",
      "detail": "Traceback ... (截断)",
      "stage": "login"
    },
    "saved": [
      {
        "name": "content_ab12.json",
        "kind": "content",
        "size": 2048,
        "save_id": "sav_xyz789",
        "delivery": "delivered",
        "remote_path": "s3://cdc-data/proj_xxx/run_abc123/content_ab12.json"
      }
    ],
    "next_run_at": 1750001800000
  }
}
```

- `event=run.started` 时：`status=running`，`finished_at/duration_ms/error/saved` 为空，`next_run_at` 可为空。
- `event=run.succeeded` 时：`status=succeeded`，`error=null`，`saved` 为本次产物索引。
- `event=run.failed` 时：`status=failed`，必带 `error`。
- `next_run_at` 仅 cron 模式在结束事件中提供。
- `saved[].delivery`：`delivered`（CDC 已确认）/ `failed`（重试耗尽）；启用数据 webhook 后
  逐条对应 7.12 的 `data.saved` 推送，`remote_path` 为 CDC 落库位置。

**error.type 取值**

| type | 含义 |
| --- | --- |
| `login_failed` | 登录失败（含取消、验证码失败） |
| `login_not_saved` | 登录成功但未保存凭据（login 模式） |
| `login_timeout` | 登录超时 |
| `verification_failed` | 人机验证限次未通过 |
| `code_error` | 脚本运行异常 |
| `no_code` | 无可用代码 |
| `browser_error` | 浏览器启动/运行异常 |
| `internal_error` | 其他内部错误 |

### 7.8 webhook 返回（响应）设计

CDC 接收端返回：

**成功（HTTP 200）**

```json
{
  "ok": true,
  "delivery_id": "dlv_9f2c...",
  "received_event": "run.failed",
  "run_id": "run_abc123",
  "accepted": true
}
```

**幂等重复（HTTP 200）**：`accepted=false` + `reason="duplicate"`。

**校验失败**

| HTTP | 场景 | Body |
| --- | --- | --- |
| 400 | 报文格式错误 | `{"ok":false,"error":"invalid_payload","message":"..."}` |
| 401 | 签名校验失败 | `{"ok":false,"error":"invalid_signature"}` |
| 404 | `run_id` 不存在 | `{"ok":false,"error":"run_not_found"}` |
| 409 | 事件状态非法（如 succeeded→failed） | `{"ok":false,"error":"state_conflict"}` |
| 429 | 频率限制 | `{"ok":false,"error":"rate_limited","retry_after_ms":5000}` |
| 5xx | CDC 内部错误 | `{"ok":false,"error":"internal_error"}` |

ACS 对非 2xx 或网络错误按指数退避重试（如 1s/5s/30s，最多 5 次），
每次记录 `webhook_deliveries`；仍失败则写日志并（可选）进入死信。

### 7.9 重试 / 签名 / 幂等

- **签名**：HMAC-SHA256，密钥由 CDC 下发（`WEBHOOK_SECRET`），支持轮换。
- **幂等**：`X-Webhook-Delivery` + `(run_id, event)` 唯一约束，重复直接 200 duplicate。
- **顺序**：同一 run 的 started 必先于 succeeded/failed；CDC 校验状态迁移。
- **安全**：仅允许 https，建议内网地址白名单，避免 SSRF。

### 7.10 登录失败归类

- 运行期任何 `page_login` 失败 / `LoginCancelled` / `login_timeout` / `VerificationFailed`，
  统一映射为 `run.failed` 且 `error.type ∈ {login_failed, login_timeout, verification_failed}`。
- CDC 可将该 run 的 `status` 置 `failed`，并触发告警/重试策略。

### 7.11 定时任务心跳与存活检测（cron）

cron 模式是**长驻进程**，仅靠运行 webhook 无法判断进程是否存活。CDC 需要通过 ACS
周期心跳来验证该定时任务进程的**存活（liveness）**与**健康（health）**。

#### 7.11.1 心跳机制（ACS → CDC）

- 发送方：ACS `backend/services/runmode/heartbeat.py`，由 cron 主循环以独立后台任务周期发送。
- 接收方：CDC `POST /api/v1/webhooks/heartbeat`（见 3.6）。
- 间隔：`--heartbeat-interval`（默认 30s）；发送为**尽力而为**，不阻塞调度主循环。
- 认证：复用 webhook 的 HMAC 签名与 `X-Webhook-*` 头（见 7.9），并带 `X-Heartbeat-Id`。
- 启动即发一次；每次运行开始/结束时立即补发一次，使状态实时刷新。
- 优雅退出时发送一次 `status=stopping`，供 CDC 区分「主动停止」与「异常失联」。

#### 7.11.2 心跳报文

```json
{
  "crawler_id": "proj_xxx",
  "process_id": "proc_abc123",
  "project_id": "p_123",
  "workspace_id": "ws_456",
  "mode": "run",
  "run_type": "cron",
  "cron": "*/30 * * * *",
  "pod_name": "acs-run-proj-xxx-0",
  "status": "alive",
  "pid": 12345,
  "started_at": 1750000000000,
  "uptime_ms": 3600000,
  "next_run_at": 1750001800000,
  "last_run": {
    "run_id": "run_abc123",
    "status": "succeeded",
    "started_at": 1749999990000,
    "finished_at": 1750000000000
  },
  "health": {
    "browser": true,
    "mongo": true,
    "llm": true,
    "disk_ok": true,
    "last_error": null
  },
  "version": "0.1.0"
}
```

- `status`：`alive`（存活且空闲）/ `running`（正在执行一次爬取）/ `degraded`（存活但健康项异常）/ `stopping`。
- `health`：各依赖可用性，用于区分「进程活着」与「进程可用」。

#### 7.11.3 CDC 侧存活/健康判定

- 接收心跳后更新 `run_processes`：`last_heartbeat_at`、`status`、`health`、`next_run_at`、`last_run`。
- **存活判定（liveness）**：后台巡检任务（如每 10s）检查
  `now - last_heartbeat_at > max(3 × interval, 90s)` → 置 `status=unreachable`（失联），
  触发告警，并按策略重启 Pod（删除重建 Deployment Pod）。
- **健康判定（health）**：心跳中 `health.*` 任一为 false → 置 `degraded`，告警但不重启；
  连续多次 `degraded` 可升级为重启。
- **状态迁移**：`alive/running → unreachable`（超时）；`unreachable → alive`（心跳恢复）；
  `stopping → stopped`（收到停止心跳或 Pod 正常退出）。
- CDC 前端在项目页展示进程状态、最后心跳时间、下次运行时间、最近一次运行结果。

#### 7.11.4 与 K8s 探针的关系

- **K8s liveness/readiness 探针**：容器级，`GET /healthz`（ACS 进程内），负责「进程卡死则重启」。
- **CDC 心跳**：控制面级，负责「进程失联/降级」的观测、告警与跨集群调度决策。
- 两者互补：探针保证单容器自愈，心跳保证 CDC 对调度任务的全局可见与可运维。

#### 7.11.5 容错

- ACS 心跳失败不自杀（避免 CDC 短时不可达导致误停），仅本地记录；
  如需「心跳失败即退出」可加 `--exit-on-heartbeat-fail`（默认关闭）。
- CDC 失联告警阈值、重启策略、连续降级阈值均可配置，并写入 `audit_logs`。

### 7.12 爬取数据 webhook 推送（save_content）

**在提供数据 webhook 地址的情况下，run 模式每次执行 `save_content(...)` 都进行一次 webhook 推送**，
把数据交给 CDC 落库/落对象存储；CDC 是数据的持久化方，ACS 只负责采集与推送。

#### 7.12.1 触发点与复用

- 触发点：`CrawlerEnv.save_content(data, fmt)`（`backend/services/crawler.py`）内部，保存成功后推送；
  `save_page()` 采用同一机制（可选，默认同样推送）。
- 仅在 `MODE=run` 且配置了数据 webhook 时启用；未配置时保持现状（仅本地 `output/` 落盘）。
- 登录模式不涉及业务数据，不推送（登录凭据仍走既有 `set_login_ticket`）。

#### 7.12.2 配置

| 参数 | env | 默认 | 说明 |
| --- | --- | --- | --- |
| `--data-webhook-url` | `DATA_WEBHOOK_URL` | 由 `WEBHOOK_URL` 推导 | 数据推送地址（默认同源 `/webhooks/data`）；为空则关闭推送 |
| `--data-inline-max-bytes` | `DATA_INLINE_MAX_BYTES` | `1048576` | ≤ 该值走内联 JSON，超过走预签名直传 |
| `--data-webhook-sync` | `DATA_WEBHOOK_SYNC` | `0` | `1`=`save_content` 同步等待 CDC 确认；`0`=异步队列（默认） |
| `--data-webhook-secret` | `DATA_WEBHOOK_SECRET` | 复用 `WEBHOOK_SECRET` | HMAC 签名密钥 |

#### 7.12.3 推送流程（ACS 侧）

```
save_content(data, fmt)
  → 本地规范化(沿用既有 normalize_fmt / cap_text_bytes / 图片 base64 解码)
  → 生成 save_id(稳定) 与 content_hash(sha256)
  → 若 size <= inline 阈值：POST /webhooks/data（内联 content）
    否则：POST /webhooks/data/prepare → 预签名 PUT 原始字节 → POST /webhooks/data/commit
  → 异步模式：入有界队列由后台 sender 重试投递；同步模式：await 确认
  → 记录 {save_id, remote_path, status} 到本次运行上下文
  → 返回本地路径(保持既有返回契约不变)
```

- 发送方：新增 `backend/services/runmode/data_webhook.py`（复用 `webhook.py` 的签名/重试/幂等）。
- 顺序：同一 run 内带单调递增 `seq`，CDC 按 `seq` 保序落库。
- **一致性**：run 结束事件（`run.succeeded`）必须在数据队列 **flush 且全部确认** 之后发送；
  若仍有未确认项，则 `run.failed`/`run.succeeded` 的 `saved[]` 中标记 `delivery: "failed"` 并附错误。

#### 7.12.4 数据报文（请求）

**Headers**：同 7.9（`X-Webhook-Event: data.saved`、`X-Webhook-Delivery`、`X-Webhook-Signature`、`X-Webhook-Timestamp`）。

**Body（内联）**

```json
{
  "event": "data.saved",
  "delivery_id": "dlv_7a1b...",
  "timestamp": 1750000000000,
  "data": {
    "crawler_id": "proj_xxx",
    "project_id": "p_123",
    "workspace_id": "ws_456",
    "run_id": "run_abc123",
    "save_id": "sav_xyz789",
    "seq": 3,
    "kind": "content",
    "fmt": "json",
    "name": "content_ab12.json",
    "size": 2048,
    "content_hash": "sha256:9f2c...",
    "content_type": "application/json",
    "encoding": "utf-8",
    "content": "{\"title\":\"...\"}",
    "source_url": "https://target.example.com/list",
    "collected_at": 1750000000000
  }
}
```

- `encoding`：`utf-8`（文本/json/jsonl/csv）或 `base64`（img/二进制）。
- 大文件（> `--data-inline-max-bytes`）：`data.storage = {"mode":"presigned","upload_url":"https://...","expires_at":...}`，
  正文不在 JSON 内，由 ACS 直传对象存储后 `commit`。

#### 7.12.5 数据报文（返回）

**成功（200）**

```json
{
  "ok": true,
  "accepted": true,
  "save_id": "sav_xyz789",
  "run_id": "run_abc123",
  "stored": true,
  "storage": { "backend": "s3", "path": "s3://cdc-data/proj_xxx/run_abc123/content_ab12.json" },
  "received_at": 1750000000000
}
```

| HTTP | 场景 | Body |
| --- | --- | --- |
| 200 | 幂等重复 | `{"ok":true,"accepted":false,"reason":"duplicate","save_id":"..."}` |
| 400 | 报文非法 | `{"ok":false,"error":"invalid_payload"}` |
| 401 | 签名失败 | `{"ok":false,"error":"invalid_signature"}` |
| 413 | 超出单次上限 | `{"ok":false,"error":"payload_too_large","max_bytes":...}` |
| 429 | 限流 | `{"ok":false,"error":"rate_limited","retry_after_ms":5000}` |
| 507 | 存储配额不足 | `{"ok":false,"error":"storage_quota_exceeded"}` |
| 5xx | CDC 内部错误 | `{"ok":false,"error":"internal_error"}` |

- 幂等键：`(run_id, save_id)`；`content_hash` 相同视为同一数据，CDC 可去重不重复落库。
- ACS 对非 2xx 按指数退避重试（默认最多 5 次）；耗尽后标记该条 `delivery=failed`。

#### 7.12.6 CDC 侧处理

- 接收端点：`POST /api/v1/webhooks/data`（内联）与 `POST /api/v1/webhooks/data/prepare|commit`（预签名）。
- 校验签名 → 幂等去重 → 按 `seq` 落库到对象存储 + `data_items` 索引集合。
- 新集合 `data_items`：`{save_id, run_id, crawler_id, project_id, workspace_id, seq, kind, fmt, name, size, content_hash, storage_backend, storage_path, source_url, collected_at}`。
- 写入后更新 `runs.saved[]` 与项目数据量配额；供 CDC 前端数据浏览页查询/下载。

---

## 8. 前端改造

### 8.1 CDC 前端（新项目）

- 技术栈建议与 ACS 前端一致（React + Vite + TS）。
- 页面：登录/注册、工作空间列表、成员与角色、项目列表、项目详情、**loading 页**、
  运行记录、数据浏览、webhook 投递记录、审计。
- loading 页轮询开发容器状态并处理失败重试。

### 8.2 ACS 前端（本项目）

- 按 `mode` 渲染：
  - `dev`：现状（完整 IDE）。
  - `login`：只读状态页 + **内嵌只读实时画面**（必选，见 6.6；无登录表单/操作按钮），**隐藏编辑器/Agent/版本/导出**。
  - `run`：不提供 UI。
- 模式可由后端在 `index.html` 注入 meta（类似现有 `acs-api-prefix`）或新增 `/api/v1/mode`。

---

## 9. 安全设计

| 维度 | 措施 |
| --- | --- |
| 认证 | JWT + Refresh；容器内不信任前端传入的 `crawler_id` |
| 授权 | RBAC 中间件按 scope 校验 |
| 凭据 | `login_tickets` 加密存储（KMS/字段级加密），日志脱敏 |
| 容器 | Pod 非 root、只读根文件系统、NetworkPolicy、资源限制 |
| 网络 | 开发容器仅经 CDC 反向代理暴露，禁止直接公网 |
| webhook | HMAC 签名 + 时间戳防重放 + 内网白名单 |
| 审计 | 所有敏感操作写 `audit_logs` |
| 密钥 | LLM/Webhook/Mongo 密钥走 K8s Secret，不落镜像 |

---

## 10. 实施计划（里程碑）

| 阶段 | 内容 | 交付 |
| --- | --- | --- |
| M0 基础 | CDC 项目脚手架、用户系统、JWT、Mongo 连接 | 可登录 |
| M1 权限与空间 | RBAC、工作空间、成员管理 | 权限可用 |
| M2 项目 | 爬虫项目 CRUD、来源选择、`crawler_id` 分配 | 项目可用 |
| M3 开发容器 | K8s 编排、Pod 模板、Service/Ingress、loading 页、ACS `/healthz` + `MODE=dev` | 可进入开发容器 |
| M4 login 模式 | ACS `MODE=login`（复用既有登录/凭据流程）、结果/退出码、CDC 内嵌只读实时画面代理 | 登录可完成（含人工扫码） |
| M5 run 模式 | `MODE=run`、once/cron、调度、webhook 发送与接收、**cron 心跳与存活/健康检测**、**`save_content` 数据逐条推送（含大文件预签名）** | 定时运行 + 回调 + 数据入库 + 进程可观测 |
| M6 结果与数据 | 运行记录、结果索引、`data_items` 数据浏览/下载、webhook 投递记录 | 数据可查 |
| M7 内置爬取器 | builtin 来源接入（预留接口落地） | 内置爬取可用 |
| M8 加固 | 审计、配额、回收、告警、压测 | 生产就绪 |

**建议顺序**：先做 M0–M3（打通开发容器闭环），再做 M4/M5（运行模式），最后 M6–M8。

---

## 11. 测试策略

- **CDC**：单测（RBAC/状态机/webhook 校验）+ 集成（容器编排用 fake K8s client）。
- **ACS 改造**：沿用 `tests/`（pytest + pytest-asyncio + mongomock）：
  - `mode` 分发与路由门禁测试；
  - `login_mode` 结束判定 / 超时 / 未保存 分支（验证复用既有登录链路）；
  - `run_mode` once/cron 循环（注入 fake clock）；
  - `webhook` 签名、重试、幂等、失败映射；
  - `/healthz` 就绪逻辑。
- **端到端**：本地 kind/minikube 拉起 dev/login/run 三类 Pod，验证 loading 解除与 webhook 往返。
- 保持后端覆盖率 ≥ 90%（`pyproject.toml` 已配置）。

---

## 12. 风险与待定问题

| # | 问题 | 备选/建议 |
| --- | --- | --- |
| 1 | login 内嵌实时画面的代理方式 | **已定**：CDC 反向代理 WebSocket + 短时效令牌（见 6.6.2）；备选为 K8s port-forward |
| 2 | 「登录完成」如何精确判定 | **已定**：复用既有链路，`page_login` 成功 + 既有 `set_login_ticket` 保存成功即结束（见 6.3），不新增哨兵 |
| 3 | 运行结果数据存哪 | **已定**：每次 `save_content` 经 `data.saved` webhook 推送，CDC 落对象存储 + `data_items`；大文件预签名直传（见 7.12） |
| 4 | cron 由谁调度 | 建议 ACS 进程内调度（复用 `cron.py`），CDC 只负责启停 Pod |
| 5 | 多工作空间共享 Mongo 的隔离 | 靠 `crawler_id` + 应用层校验；如需强隔离可按空间分库 |
| 6 | webhook / 心跳可达性 | ACS 出站需允许访问 CDC；内网域名 + 重试 + 死信；心跳失败不自杀，由 CDC 侧超时告警/重启 |
| 7 | 镜像体积与启动时延 | 预装 Chrome/Playwright 依赖，优化镜像分层与就绪探针 |
| 8 | 开发容器并发配额 | CDC 侧按空间限流，超限返回 `QUOTA_EXCEEDED` |

---

## 13. 附录

### 13.1 ACS 运行模式 CLI 速查

```bash
# dev（现状）
uv run python -m backend.main --crawler-id proj_xxx

# login：自动读取 Mongo HEAD，登录+保存后退出
MODE=login CRAWLER_ID=proj_xxx MONGO_URI=... \
  uv run python -m backend.main --login-timeout 300

# run：一次性 + 运行回调 + 数据逐条推送
MODE=run RUN_TYPE=once CRAWLER_ID=proj_xxx RUN_ID=run_abc \
  WEBHOOK_URL=https://cdc/api/v1/webhooks/runs \
  DATA_WEBHOOK_URL=https://cdc/api/v1/webhooks/data \
  WEBHOOK_SECRET=*** \
  uv run python -m backend.main

# run：定时（含心跳探活 + 数据推送）
MODE=run RUN_TYPE=cron CRON="*/30 * * * *" CRAWLER_ID=proj_xxx \
  WEBHOOK_URL=https://cdc/api/v1/webhooks/runs \
  DATA_WEBHOOK_URL=https://cdc/api/v1/webhooks/data \
  HEARTBEAT_INTERVAL=30 \
  uv run python -m backend.main
```

### 13.2 关键既有代码锚点

| 能力 | 位置 |
| --- | --- |
| 配置/CLI | `backend/config.py` |
| 应用工厂/入口 | `backend/main.py` |
| 代码版本（Mongo） | `backend/services/code_version.py` |
| 登录凭据（Mongo） | `backend/services/crawler.py`（`get/set_login_ticket`） |
| 代码执行环境 | `backend/services/browser.py:428`（`run_code`）、`backend/services/crawler.py`（`CrawlerEnv`） |
| 登录协作 | `backend/services/agent/login.py`、`backend/services/agent/run_login.py` |
| cron 校验/计算 | `backend/services/cron.py` |
| 导出运行时（可参考） | `backend/services/exporter.py`（`RUNTIME_PY` / `MAIN_PY`） |
| 会话持久化 | `backend/services/agent/session/store.py` |
