# 代码变更管理与持久化设计文档

> 目标：为前端代码编辑器新增「类 Git」的变更管理与持久化能力。
> 未提交的变更暂存在**浏览器存储**（`localStorage`），已提交的历史保存在 **MongoDB**。
> 后端提供提交 / 历史查询 / 检出等接口，前端提供「源码管理」面板与自动保存。

---

## 1. 背景与目标

当前编辑器代码仅做两层保存：

- 前端 `App.tsx` 防抖同步到后端内存镜像（`EditorState`），刷新页面即丢失；
- Agent 可通过 `set_editor_code` 回写代码，但无历史概念。

用户需要：

1. **未提交变更（工作区）**：编辑器内容持续自动暂存到浏览器存储，刷新/重开页面不丢失；
2. **已提交版本（历史）**：可将当前代码作为一次「提交」固化到 MongoDB，形成可回溯、可比较、可恢复的历史链；
3. 交互模型对齐 Git：工作区 ↔ 提交链、HEAD、提交信息、相对 HEAD 的未提交差异（`+N -M`）。

### 非目标

- 不做真正的文件系统 / 多文件仓库管理（当前编辑器是单一 Python 脚本）；
- 不做真正的分支管理（`rebase` / 多分支 `merge`），提交链保持线性；
- 但**刷新/重开页面时的三方合并**在范围内：当「浏览器暂存的草稿」与「刷新后已变更的代码」出现分歧时，提供行级自动合并与受控的冲突解决（保留暂存 / 保留最新 / 保留两者），绝不在加载时静默覆盖；
- 不做鉴权体系（沿用现有 `crawler_id` 隔离模型）。

---

## 2. 概念模型（Git 语义映射）

| Git 概念 | 本项目对应 | 存放位置 | 说明 |
| --- | --- | --- | --- |
| 工作区 Working Tree | 前端编辑器当前内容 | 浏览器内存 + `localStorage` 草稿 | 用户每次输入即更新，防抖自动暂存 |
| 暂存区 Index | （不引入） | — | 单文件场景下暂存区与工作区等价，提交即取工作区全量 |
| 提交 Commit | 一次源码快照 | MongoDB `code_commits` | 不可变：`commit_id` 由内容 + 父提交 + 消息 + 时间哈希 |
| HEAD | 当前最新提交 | MongoDB `code_repos`（按 `crawler_id` 单文档） | 记录最新 `commit_id` |
| 历史 History | 提交链 | MongoDB（`parent` 指针 + `created_at` 排序） | 前端列表展示 |
| 工作区差异 | 工作区 vs HEAD 的 diff | 前端实时计算 | 复用 `frontend/src/utils/diff.ts` 的 `diffHunks` |
| 检出 Checkout | 用某次提交内容覆盖工作区 | 后端返回内容 → 前端写回编辑器 | 等价 `git checkout <commit> -- <file>`，不移动 HEAD |
| 刷新合并 | 刷新时「暂存草稿」与「已变更代码」的分歧 | 浏览器 + 前端实时计算 | 三方合并 `merge3(base=草稿.head 提交, mine=草稿, theirs=最新 HEAD/镜像)` |
| 分支 Branch | 本期不引入 | — | HEAD 始终指向最新提交，提交链线性 |

关键规则：

- **工作区的唯一权威在浏览器**（用户正在编辑的东西）；后端镜像 `EditorState` 仅作为 Agent 读/写的辅助，不参与版本判断；
- **刷新后代码可能已变更**：草稿基于旧的 `head` 提交；刷新/重开后 Mongo 的 HEAD（他人提交）或后端镜像（Agent `set_editor_code` 回写）可能已前移，加载时必须做三方合并，不能拿旧草稿静默覆盖新代码；
- **提交是唯一进入 MongoDB 的入口**，提交时前端把工作区全量内容显式上传；
- 每次提交后，HEAD 前移到新提交；工作区内容与 HEAD 相等 → `dirty = false`。

---

## 3. 数据存储设计

### 3.1 浏览器存储（未提交变更）

- **Key 设计**：`crawlerCode:draft:{crawler_id}`（`crawler_id` 缺省用 `"default"`），与后端会话隔离规则一致，避免不同爬虫实例互相覆盖；刷新/重开后以草稿的 `head` 为三方合并基点（见 6.4）。
- **Value 结构**（JSON）：

  ```json
  {
    "content": "<编辑器全量源码>",
    "saved_at": 1735700000000,
    "head": "a1b2c3...",      // 保存时 HEAD 的 commit_id; 用于刷新后判断 dirty, 也是三方合并的基点(base)
    "author": "dev"
  }
  ```

- **写入策略**：编辑器内容变化后防抖（约 500ms）写入 `localStorage`；与现有 `setEditorCode`（800ms 同步后端镜像）并行，互不干扰。
- **容量与降级**：`localStorage` 单条约 5MB。写入捕获 `QuotaExceededError`：
  - 内容 < 1MB：正常持久化；
  - 内容 ≥ 1MB 或配额超限：跳过草稿持久化，并在「源码管理」面板提示「草稿未保存（本地存储容量不足）」，内存工作区不受影响；
  - 后续里程碑可迁移到 IndexedDB 存草稿（>1MB 也稳定）。
- **恢复时机**：前端加载时若 `localStorage` 有草稿且内容非空，先做**刷新后合并**（见 6.4），而不是无条件恢复草稿：
  - 无外部变更（当前 HEAD / 后端镜像与草稿基点一致）→ 直接恢复草稿；
  - 有变更但可自动合并（无冲突）→ 恢复合并结果并更新草稿；
  - 有冲突 → 进入冲突解决界面，绝不静默覆盖。
- **命名空间与凭据隔离**：浏览器端所有持久化键都必须以 `crawler_id` 为命名空间（缺省 `"default"`），同一台浏览器上不同爬虫实例互不读取、互不覆盖，避免串草稿 / 串凭据：

  | 键 | 内容 | 隔离粒度 |
  | --- | --- | --- |
  | `crawlerCode:draft:{cid}` | 未提交草稿（含 `head` 基点） | 每 crawler |
  | `crawlerCode:author:{cid}` | 提交人名称（首次填写后记住） | 每 crawler |
  | `agent.unread.{cid}` / `agent.seen.{cid}` | Agent 未读角标（现状已有） | 每 crawler |
  | `crawlerCode:cred:{cid}` | 后续里程碑可能持久化的目标站登录凭据快照 | 每 crawler |

### 3.2 MongoDB（已提交版本）

沿用项目已有的 motor 异步驱动 + 快速失败/冷却模式（参考 `backend/services/agent/session/store.py`）。新增两个集合：

#### `code_commits` — 提交快照（不可变）

```json
{
  "_id": ObjectId,
  "crawler_id": "dev_test",
  "commit_id": "3f9a...c2e1",      // sha1(crawler_id + parent + message + content + created_at)
  "parent": "7ab0...d1f4",         // 父提交 commit_id, 首提交为 null
  "message": "修复翻页逻辑",
  "author": "dev",                 // 可选, 前端草稿里保存的名称
  "content": "<该版本完整源码>",
  "content_hash": "e5d8...",       // sha1(content), 用于快速判重/判脏
  "size": 4821,
  "created_at": 1735700000000      // epoch ms
}
```

索引：

- `(crawler_id, commit_id)` —— 唯一索引（同 crawler 内 commit_id 唯一）；
- `(crawler_id, created_at DESC)` —— 历史列表排序。

#### `code_repos` — 仓库状态（每个 crawler 一条）

```json
{
  "_id": ObjectId,
  "crawler_id": "dev_test",        // 唯一索引
  "head": "3f9a...c2e1",           // 当前 HEAD commit_id, 可为 null(空仓库)
  "updated_at": 1735700000000
}
```

> 说明：`head` 在正常使用中等于最新一条提交；显式存储是为后续支持「回退 HEAD」等能力留口子，也让「当前指向哪个版本」一目了然。

---

## 4. 后端接口设计

新增 `backend/services/code_version.py`（`CodeStore`，motor 异步，含 `_connect` 快速失败 + 冷却）与 `backend/routers/versions.py`（前缀 `/api/v1`），在 `backend/main.py` 注册。

所有接口按 `crawler_id` 隔离，缺省回退 `"default"`（与 Agent 会话一致）。MongoDB 不可达时：
- 读接口返回空列表 / `head=null`（优雅降级）；
- 写接口返回 `503` 与明确中文错误，前端保留浏览器草稿不丢数据。

### 4.1 `GET /code/repo`

查询仓库状态。

**响应：**

```json
{
  "crawler_id": "dev_test",
  "has_commits": true,
  "head": {
    "commit_id": "3f9a...c2e1",
    "message": "修复翻页逻辑",
    "created_at": 1735700000000
  }
}
```

### 4.2 `POST /code/commit`

把工作区内容固化为一次提交。

**请求：**

```json
{
  "message": "修复翻页逻辑",       // 必填, 非空且长度 ≤ 200
  "content": "<工作区完整源码>",    // 必填
  "author": "dev",                // 可选, 缺省 "unknown"
  "crawler_id": "dev_test"        // 可选, 缺省 "default"
}
```

**处理逻辑：**

1. 取 `code_repos.head`（空仓库则 parent=null）；
2. `content_hash = sha1(content)`，若与 HEAD 的 `content_hash` 相同 → 返回 `400 {"detail":"工作区与最新提交一致, 无变更可提交"}`；
3. `commit_id = sha1(crawler_id + parent + content_hash + message + created_at)`；
4. 写入 `code_commits`，并更新 `code_repos.head = commit_id`（两步在同一连接内尽量保证顺序，允许最终一致）；
5. 返回新提交完整信息。

**响应：**

```json
{
  "ok": true,
  "commit": {
    "commit_id": "3f9a...c2e1",
    "parent": "7ab0...d1f4",
    "message": "修复翻页逻辑",
    "author": "dev",
    "created_at": 1735700000000,
    "size": 4821,
    "content": "<完整源码>"
  }
}
```

### 4.3 `GET /code/commits?crawler_id=&limit=&before=`

历史列表（按 `created_at` 倒序，默认 50 条，可选 `before` 分页）。

**响应：**

```json
{
  "commits": [
    {
      "commit_id": "3f9a...c2e1",
      "parent": "7ab0...d1f4",
      "message": "修复翻页逻辑",
      "author": "dev",
      "created_at": 1735700000000,
      "size": 4821,
      "stat": { "add": 12, "del": 3 }   // 相对父提交的变更统计(首提交统计全部行)
    }
  ]
}
```

> `stat` 由后端用 `difflib.SequenceMatcher` 在响应时计算（单文件、百级行内场景开销可接受）；如需严格行级 hunk，可在 `GET /code/commits/{id}` 上额外提供 `/diff`。

### 4.4 `GET /code/commits/{commit_id}?crawler_id=`

单次提交详情（含 `content` 全量源码，供检出 / 对比）。

### 4.5 `POST /code/checkout`

把指定提交的内容检出到工作区（不动 HEAD，等价 `git checkout <commit> -- file`）。

**请求：**

```json
{
  "commit_id": "7ab0...d1f4",
  "crawler_id": "dev_test"
}
```

**响应：**

```json
{
  "ok": true,
  "commit_id": "7ab0...d1f4",
  "code": "<该提交源码>"
}
```

前端拿到 `code` 后写回 Monaco 编辑器、更新本地草稿（`head` 保持原 HEAD 不变），于是工作区相对 HEAD 呈现「变更」状态——符合 Git 检出文件后工作区变脏的直觉。

### 4.6 可选（后续里程碑）

- `POST /code/reset`：把 HEAD 回退到指定提交（`{ target_commit_id }`），用于「撤销最后一次提交」；
- `DELETE /code/commits/{commit_id}`：仅允许删除链尾且非 HEAD 的悬空提交（保守起见默认不做）。

---

## 5. 前端设计

### 5.1 状态管理与 Hook

新增 `frontend/src/hooks/useVersions.ts`，承载：

| 状态 | 含义 | 来源 |
| --- | --- | --- |
| `draft` | 工作区内容（与 `App.tsx` 的 `code` 保持一致） | 编辑器 `onChange` |
| `head` | HEAD 提交摘要 | `GET /code/repo` |
| `commits` | 历史列表 | `GET /code/commits` |
| `dirty` | `draft` 与 HEAD 内容是否不同 | `draft.content_hash !== head.content_hash`（前端 SHA-1 或内容直接比对） |
| `author` | 提交人名称 | 读本地 `localStorage`（`crawlerCode:author:{crawler_id}`），首次填写后记住 |
| `base` | 草稿的三方合并基点内容 | `GET /code/commits/{draft.head}`（草稿 `head` 提交的 content） |
| `mergeState` | 刷新后合并状态（`none`/`merged`/`conflict`） | 加载时 `merge3(base, draft, theirs)` 的结果 |

职责：

- **加载**：读 `localStorage` 草稿 → 拉取 repo + commits + 后端镜像 → 做刷新后三方合并（`merge3`，见 6.4）：无分歧直接恢复草稿；无冲突自动合并；有冲突进入解决界面；
- **自动保存**：`draft` 变化防抖 500ms 写 `localStorage`（捕获配额异常降级）；
- **提交**：调 `POST /code/commit`，成功后刷新 `head`/`commits`，`dirty=false`；
- **检出**：调 `POST /code/checkout`，把返回的 `code` 通过 App 层写入编辑器（走与 `handleAgentCode` 相同的模型替换路径），更新 `draft` 与草稿；
- **刷新合并 / 冲突解决**：加载时 `merge3` 返回冲突时，面板逐处展示 `保留暂存 / 保留最新 / 保留两者`，确认后写回草稿与编辑器；未确认前草稿保持原样，不自动覆盖；
- **未提交差异**：`diffHunks(head.content, draft, 3)` 计算 `+N -M` 与行级高亮（复用 `utils/diff.ts`，与 AgentDiffCard 同款渲染，可抽一个公共 `DiffView` 组件）。

### 5.2 UI：新增「源码管理」面板

- **入口**：`ActivityBar` 新增 `PanelKey = "versions"`（图标沿用 VSCode 分支/版本样式），`types.ts` 的 `PanelKey` 联合类型加 `"versions"`；
- **组件**：`components/VersionsPanel.tsx`（渲染在 `Sidebar` 中），区块：
  1. **刷新合并 / 冲突**（仅在 `mergeState != none` 时出现）：横幅说明「暂存草稿与刷新后的最新代码已合并 / 存在冲突」；无冲突时一键接受合并结果，有冲突时逐处选择 `保留暂存 / 保留最新 / 保留两者`，确认后写回编辑器与草稿；
  2. **未提交变更**：`dirty` 指示（`*N 变更`），相对 HEAD 的 `+N -M`，展开显示行级 diff；空态文案「与最新提交一致」；
  3. **提交**：消息输入框 + 「提交」按钮（无变更时禁用）；提交成功后清空输入并滚动到历史顶部；
  4. **历史**：提交列表（短哈希、消息、作者、时间、`+N -M`）；每条可「检出」按钮（确认后恢复源码）、点击展开查看该版本完整 diff（相对父提交）；
  5. **仓库状态**：`crawler_id`、HEAD 短哈希、本地草稿是否已持久化（容量不足时提示）。
- **样式**：沿用现有 VSCode 暗色布局与 `oc-diff` 类名。

### 5.3 与 App 主流程的集成

- `App.tsx` 现有 `code` 状态与 `handleAgentCode`（Agent 回写路径）继续复用；新增把 `code` 的变化通知给 `useVersions`（或由 `useVersions` 作为 `code` 的唯一持有者，App 订阅其 draft）；
- **建议实现**：`useVersions` 持有 `draft`，通过 `onDraftChange` 把内容交给 `CodeEditor`；提交/检出操作由面板发起；
- Agent 的 `set_editor_code` 回写 → 工作区变化 → 自动计入「未提交变更」并持久化草稿，与「已提交在 Mongo / 未提交在浏览器」的边界天然一致；
- **可选联动**：检出时同步更新后端 `EditorState`（复用现有 `POST /editor/code`），保证 Agent 读取的镜像与工作区一致；
- 刷新合并 / 冲突解决确认后，同样同步 `POST /editor/code`，避免 Agent 继续基于旧的镜像代码工作。

---

## 6. 关键流程时序

### 6.1 自动保存（工作区 → 浏览器）

```
用户输入 --> Monaco onChange --> App(code) --> useVersions.draft
                                      │
                                      ├─ 防抖 500ms ─> localStorage['crawlerCode:draft:{cid}']
                                      └─ 防抖 800ms ─> POST /editor/code (Agent 镜像, 现状保留)
```

### 6.2 提交（工作区 → MongoDB）

```
面板点击「提交」→ 输入 message
  → POST /code/commit { message, content: draft, author }
      → CodeStore 写 code_commits + 更新 code_repos.head
  → 返回 commit → 刷新 head/commits → dirty=false → 清空消息输入
```

### 6.3 检出（MongoDB → 工作区）

```
历史列表点击「检出」
  → 确认 → POST /code/checkout { commit_id }
  → 返回 code → 写回 Monaco 模型 + 更新 draft + 更新 localStorage 草稿(head 不变)
  → 面板显示相对 HEAD 的未提交差异(检出内容 vs HEAD)
```

### 6.4 页面刷新 / 重开（含「代码已变更」的合并）

刷新/重开后，草稿基于旧的 `head` 提交，而「最新代码」可能已变：他人（其他标签页/浏览器）提交前移了 Mongo HEAD，或 Agent 在本页关闭期间用 `set_editor_code` 回写了后端镜像。加载时不能拿旧草稿静默覆盖，需三方合并：

```
App 加载 → useVersions 读取 localStorage 草稿 → 拉取 repo/commits + 后端镜像
  ├─ 无草稿: 取当前 HEAD(如有)作为工作区; 否则空编辑器
  └─ 有草稿:
       base   = 草稿.head 对应提交的 content(拉不到时降级为整文件冲突)
       theirs = 最新权威代码:
                  ├─ 当前 HEAD ≠ 草稿.head        → 当前 HEAD 的 content(已提交, 优先)
                  ├─ 否则后端镜像 ≠ base 且 ≠ 草稿.content → 后端镜像(Agent 回写)
                  └─ 否则                        → base(无外部变更)
       ├─ base == theirs: 无外部变更 → 直接恢复草稿
       └─ merge3(base, 草稿.content, theirs):
            ├─ 无冲突 → 恢复合并结果, 更新草稿与编辑器, 提示「已合并」
            └─ 有冲突 → 不自动写回; 进入冲突解决界面
                 逐处选「保留暂存 / 保留最新 / 保留两者」→ 写回草稿与编辑器
```

合并确认后（自动或手动），把结果 `POST /editor/code` 同步后端镜像，并刷新 `dirty`（相对当前 HEAD）。

---

## 7. 一致性、冲突与边界情况

| 场景 | 行为 |
| --- | --- |
| 空仓库首次提交 | `parent=null`，HEAD 指向首提交 |
| 内容与 HEAD 相同 | `POST /code/commit` 返回 `400 无变更可提交` |
| 提交信息为空 / 超长 | 后端校验：非空且 ≤ 200 字符，否则 `422` |
| MongoDB 不可达 | 写接口 `503` 明确报错；浏览器草稿不受影响；读接口降级为空 |
| 两个浏览器同时提交 | `commit_id` 含时间戳 + 内容哈希，唯一索引防冲突；`head` 后者覆盖前者（最终一致，本期不处理合并） |
| 检出后不提交又刷新 | 草稿 `head` 仍为原 HEAD → 刷新后恢复工作区且正确显示 dirty |
| 刷新后 HEAD 已前移 / 后端镜像已被 Agent 回写 | 三方合并：无冲突自动合并；有冲突进入解决界面，绝不静默覆盖（见 6.4） |
| 合并后再次提交 | 合并结果作为新工作区提交，与普通提交一致；提交后 `dirty=false` |
| 草稿 `head` 提交缺失（被 reset / 手动清理） | `base` 拉取 404 → 降级为整文件冲突，提示手动选择 |
| 冲突解决中又刷新 | 冲突状态为临时 UI 状态不入 `localStorage`；草稿保持原样，刷新后重新合并 |
| 多标签页同时打开同一 crawler | 各自维护草稿，合并/提交以「最后一次写回」生效；`commit_id` 含时间戳 + 内容哈希防撞 |
| `localStorage` 配额满 | 捕获异常，跳过草稿持久化并提示；内存工作区继续可用 |
| 草稿损坏（非法 JSON） | 忽略草稿，回退到 HEAD / 空编辑器，不抛错 |
| 提交内容极大 | 单文件 Python 脚本通常 < 1MB，MongoDB BSON 16MB 上限内安全；超限时 `413` |

---

## 8. 安全与隔离

- 全部按 `crawler_id` 隔离（与登录凭据、Agent 会话同一模型），缺省回退 `"default"`；
- 浏览器端持久化一律以 `crawler_id` 为命名空间（见 3.1 键表）：未提交草稿、作者名、未读角标，以及后续可能落地的目标站登录凭据快照，键都带 `{crawler_id}` 后缀，不同爬虫实例互不读取、互不覆盖；
- `commit_id` / `content_hash` 采用 `sha1`，仅用于幂等与去重，不做加密用途；
- 不新增任何密钥或敏感配置；提交内容即用户源码，权限模型沿用现有后端（无用户体系）。

---

## 9. 测试计划

### 后端（pytest + mongomock，参考现有模式）

- `tests/test_code_version.py`：
  - `CodeStore`：提交 / 列表 / 查详情 / 判重 / HEAD 更新 / MongoDB 故障冷却降级；
  - 提交哈希确定性、`content_hash` 判重、空仓库 parent=null；
  - 路由（`make_test_app` + `httpx.ASGITransport`）：`/code/repo`、`/code/commit`（含 400 无变更、422 空消息）、`/code/commits`、`/code/commits/{id}`、`/code/checkout`（含 404）；
  - `stat` 相对父提交的 `+N -M` 正确性；
  - 错误路径：Mongo 不可达时读降级 / 写 503。
- `tests/test_schemas.py`：新增请求/响应模型的必填与长度约束。

### 前端

- 无单测框架；以 `./build.sh`（`tsc -b && vite build`）保证类型通过；
- 手工验证用例：输入→刷新恢复、提交→历史出现→检出→差异显示→再次提交、配额满降级提示、MongoDB 停服时提交报错但草稿仍在。

---

## 10. 实施步骤（里程碑）

### M1 后端骨架（可独立交付）
1. `backend/services/code_version.py`：`CodeStore`（motor + 快速失败/冷却）；
2. `backend/routers/versions.py`：repo / commit / commits / commit-detail / checkout；
3. `backend/schemas.py` 新增请求/响应模型；`main.py` 注册路由；
4. 后端单测 + 覆盖率达标（仓库要求后端 90%+）。

### M2 前端基础
5. `useVersions.ts`：草稿读写、draft/head/commits 状态、commit/checkout 动作；
6. `VersionsPanel.tsx` + `ActivityBar` 新入口 + `types.ts` `PanelKey`；
7. App 集成（draft 与 `code` 状态打通、检出写回编辑器）。

### M3 体验完善
8. 未提交 diff 行级展示（复用/抽取 `DiffView`）；
9. 刷新后三方合并：`utils/merge.ts`（`merge3`）+ 6.4 加载时序 + 冲突解决界面（保留暂存/最新/两者）；
10. 检出确认、提交成功反馈、本地容量降级提示；
11. 检出 / 合并后同步 `EditorState`（Agent 镜像一致性）。

### M4（可选）
11. `POST /code/reset` 回退 HEAD；IndexedDB 大草稿；提交历史分页。

---

## 11. 涉及文件清单

| 类型 | 文件 | 动作 |
| --- | --- | --- |
| 后端服务 | `backend/services/code_version.py` | 新增 |
| 后端路由 | `backend/routers/versions.py` | 新增 |
| 后端模型 | `backend/schemas.py` | 修改（新增模型） |
| 后端入口 | `backend/main.py` | 修改（注册路由） |
| 后端测试 | `tests/test_code_version.py`、`tests/test_schemas.py` | 新增/修改 |
| 前端 Hook | `frontend/src/hooks/useVersions.ts` | 新增 |
| 前端工具 | `frontend/src/utils/merge.ts` | 新增（三方行级合并 `merge3`） |
| 前端组件 | `frontend/src/components/VersionsPanel.tsx` | 新增 |
| 前端组件 | `frontend/src/components/ConflictResolve.tsx` | 新增（冲突解决界面，可并入 VersionsPanel） |
| 前端类型 | `frontend/src/types.ts`（`PanelKey`） | 修改 |
| 前端工具 | `frontend/src/utils/api.ts` | 修改（新增接口函数） |
| 前端入口 | `frontend/src/App.tsx`、`frontend/src/components/ActivityBar.tsx`、`Sidebar.tsx` | 修改 |
| 文档 | 本文件 | 新增 |