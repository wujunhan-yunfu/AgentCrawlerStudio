# Agent 上下文压缩（Context Compression）设计方案

> 适用代码：`backend/services/agent/`
> 依赖版本：`deepagents 0.7.28`、`langchain 1.4.6`、`langgraph 1.2.16`
> 状态：已实现（后端 `core/compression.py` + 前端事件/卡片/手动压缩）
> 主要文件：`backend/services/agent/core/compression.py`、
> `backend/services/agent/prompts/compress.md`、`backend/services/agent/agent.py`、
> `backend/services/agent/runner.py`、`frontend/src/hooks/useAgent.ts`、
> `frontend/src/components/AgentPanel.tsx`

---

## 1. 背景

### 1.1 现状：上下文为什么会越来越长

Agent 是一个**会话级多轮对话**，对话状态通过 MongoDB checkpointer 按 `thread_id = session_id` 持久化，后端重启后会从断点恢复完整消息列表（`backend/services/agent/checkpointer.py`、`backend/services/agent/runner.py:665`）。

当前没有主动裁剪上下文的逻辑，上下文按以下方式单调增长：

| 增长来源 | 说明 |
| --- | --- |
| 多轮对话 | 每轮用户消息、模型回复、工具调用与工具结果都追加到 LangGraph `messages` 状态，跨轮次保留 |
| 单轮工具循环 | `ModelCallLimitMiddleware(run_limit=160)`（`backend/services/agent/agent.py:45`）允许单轮最多 160 次模型调用，每次调用的工具结果都会留在上下文里 |
| 大工具结果 | `browser_run_code` 输出上限 3000 字符、`http_request` 响应体 6000 字符、`page_analyze` 返回完整 JSON、`browser_network` 未做统一截断（`backend/services/agent/tools/browser.py`、`http.py`） |
| 多模态截图 | `browser_screenshot` 把 JPEG/PNG 的 base64 作为 `image_url` content block 放进 `ToolMessage`（`browser.py:145-171`）；事件层做了剥离，但**对话状态里仍然保留原图**，而 token 估算默认只按 85 token/图计算，真实成本远高于此 |
| 系统提示词与工具 schema | `prompts/system.md` 674 行，加上十余个工具的 schema，属于每次调用都要发送的固定底噪 |

### 1.2 已有的防护与缺口

项目已有一些**入口级**裁剪：

- `cap_text`（`core/text.py:8`）把工具结果、事件内容按字符数截断；
- `collapse_cr`（`core/text.py:16`）折叠 tqdm 的 `\r` 进度帧，避免成百上千帧进入上下文；
- 事件落库只保留预览（`session/model.py:50-65`），避免 Mongo 文档膨胀。

但这些都只作用于“单条结果”，**不能控制累计上下文**。真正接近需求的机制其实已经存在：

`deepagents.create_deep_agent` 默认会向中间件栈注入一个 `SummarizationMiddleware`（`.venv/.../deepagents/graph.py:918`，工厂函数 `deepagents/middleware/summarization.py:1811`）：

- 有模型 profile（`model.profile["max_input_tokens"]`）时：`trigger = ("fraction", 0.85)`、`keep = ("fraction", 0.10)`；
- 没有 profile 时：`trigger = ("tokens", 170000)`、`keep = ("messages", 6)`（`compute_summarization_defaults`，`summarization.py:263`）；
- 压缩时把被淘汰的消息追加到 `/conversation_history/{session_id}.md`，再生成摘要；
- 摘要事件存到私有状态 `_summarization_event`，后续每次模型调用通过 `wrap_model_call` 重建 `[摘要] + 保留的尾部消息`，原始 `messages` 不被删除；
- 遇到 `ContextOverflowError` 会自动压缩后重试。

**但是**，直接依赖默认行为存在明显问题：

1. **阈值不可控**：DeepSeek/OpenAI 兼容模型通常没有 `max_input_tokens` profile，默认阈值是 170k token。如果实际窗口是 64k/128k，触发时已经超窗；如果窗口是 256k，又可能压缩得太晚或太频繁。
2. **token 估算偏乐观**：`count_tokens_approximately` 默认 `chars_per_token=4.0`，中文场景会显著低估 token 数；图片默认按 85 token 计，而 1280px 截图远不止。
3. **不可观测**：压缩过程对前端完全透明，用户只会看到“思考中”突然变慢，或者模型“忘掉”了前面验证过的选择器和接口。
4. **不可手动触发**：长任务中用户无法主动“整理一下上下文”。
5. **保留策略固定**：`keep=("messages", 6)` 对工具循环密集的爬虫任务偏激进，容易把刚产出的关键工具结果压掉。
6. **没有项目级审计**：摘要内容、压缩前后 token、被淘汰的消息范围都没有落库，出问题无法复盘。

### 1.3 不处理的后果

- **硬失败**：供应商返回 `context_length_exceeded` / 400，整轮任务报错（`_run_turn` 捕获后进入 `error`）。
- **软退化**：即使没超窗，长上下文也会带来更高延迟、更高费用，并显著降低模型对关键约束（plan、todos、登录流程）的注意力。
- **跨轮次累积**：checkpointer 会把超长上下文一直带到下一轮，问题不会自愈。

---

## 2. 目标与非目标

### 2.1 目标

1. **自动判断**：每次模型调用前估算有效上下文，超过预算阈值时触发压缩。
2. **结构化压缩**：用 LLM 把旧历史压缩成一份**面向爬虫任务的结构化摘要**，而不是简单截断。
3. **压缩后立即生效，并持续生效**：本次及其后所有模型调用都使用“摘要 + 保留窗口”，跨轮次、跨进程重启依然有效。
4. **保留可回溯性**：被压缩的原文 offload 到文件，并可通过 `read_file` 回查；Mongo 保留审计记录。
5. **不破坏产品语义**：前端的会话历史、执行记录、保存产物、编辑器 diff 全部保持原样，压缩只影响“模型看到的上下文”。
6. **可配置、可观测、可手动触发**：阈值/保留窗口可调；压缩事件实时推送；支持手动压缩。
7. **失败可降级**：摘要模型失败、摘要超窗、压缩后仍超窗时，有确定性的兜底路径，尽量不让整轮任务失败。

### 2.2 非目标

- 不改变 `agent_messages` 中用户可见的历史记录，不做“删除消息”的产品行为。
- 不做向量检索/长期记忆（RAG）；需要细节时用 offload 文件 + `read_file`。
- 不改造 `login` / `run` 模式（它们执行产物脚本，不跑通用 Agent 对话）。
- 不替换现有 MongoDB checkpointer，也不改变 `thread_id = session_id` 的隔离模型。
- 不做跨会话的上下文复用。

---

## 3. 核心概念与设计原则

| 概念 | 含义 |
| --- | --- |
| **模型上下文** | 真正发给 LLM 的 message 列表：`[压缩摘要] + [保留窗口]` |
| **会话历史** | LangGraph 状态里的完整 `messages`，以及 Mongo `agent_messages` 中的展示记录 |
| **压缩事件** | `_summarization_event = {cutoff_index, summary_message, file_path}`，描述“从第几条开始被摘要替代” |
| **保留窗口** | 压缩后仍然原样发送的最近 N 条消息（保证工具调用配对、最近结果可见） |
| **offload 历史** | 被淘汰的原文写入 `/conversation_history/{id}.md`，供模型按需回查 |

设计原则：

1. **压缩的是“视图”，不是“账本”**：原始消息继续保留在状态/Mongo 中，模型侧只切换为压缩视图。这样既满足“后续使用压缩后内容”，又保留复盘与 UI 能力。
2. **优先摘要，不优先截断**：截断会丢语义；摘要把“过程”折叠成“结论”，保留目标、计划、事实、决策、失败原因。
3. **保留窗口必须工具安全**：绝不能把 `AIMessage(tool_calls)` 和对应的 `ToolMessage` 切开。
4. **压缩要“够晚但不过晚”**：在接近预算时压缩，压缩后留出足够空间继续完成当前任务；同时做防抖，避免每两次调用压一次。
5. **可回退**：摘要失败不能阻断任务；压缩后仍超窗要有硬兜底。

---

## 4. 总体设计

### 4.1 分层压缩策略

```
L0 入口裁剪（已有）
   cap_text / collapse_cr / 事件预览 / save_content 落盘
        │
L1 旧消息工具参数截断（低成本，先做）
   write_file / edit_file / set_editor_code / browser_run_code 的 args
   超过阈值时只保留头部 + “已截断”标记
        │
L2 全量摘要压缩（主路径）
   旧历史 -> 结构化摘要；摘要 + 最近 N 条消息继续对话
        │
L3 溢出兜底（强触发）
   ContextOverflowError -> 强制摘要 + 中段裁剪 + 单次重试
        │
L4 硬截断（最后手段）
   只保留：首条用户目标 + 最新摘要 + 最近工具结果
```

- **L0** 已经存在，不重复建设。
- **L1/L2/L3** 由 deepagents 的 `SummarizationMiddleware` 提供骨架，本方案在其上做**策略配置 + 事件 + 审计 + 中文摘要规范**。
- **L4** 由本方案的 `SessionCompressionMiddleware` 在 L2/L3 之后兜底。

### 4.2 压缩在什么位置发生

压缩不放在 `runner._run_turn` 里，而是放在 **Agent 中间件的 `awrap_model_call`** 上：

```
runner._run_turn
  └─ agent.astream(...)
       └─ model node
            └─ SessionCompressionMiddleware.awrap_model_call
                 ├─ 取 effective = [上次摘要] + messages[cutoff:]
                 ├─ 估算 tokens（system + tools + messages）
                 ├─ 未超阈值 -> 原样调用模型
                 └─ 超阈值
                      ├─ 发送 context_compression_start
                      ├─ offload 原文 -> /conversation_history/<id>.md
                      ├─ 调摘要模型 -> 结构化摘要
                      ├─ 本次模型调用使用 [新摘要] + 保留窗口
                      └─ 返回 ExtendedModelResponse(command=update(_summarization_event))
```

这样做的好处：

- **每次模型调用前都会检查**，不会因为“本轮还没结束”而漏掉；
- 工具调用中间不插入额外节点，`AIMessage(tool_calls)` 与 `ToolMessage` 的配对由中间件保证；
- 压缩结果作为图状态更新持久化到 checkpointer，**下一轮、重启后继续生效**；
- 与现有 `AgentEventMiddleware`、`TodoListMiddleware` 解耦；
- 压缩只改变“上下文”，**不会重置** `ModelCallLimitMiddleware(run_limit=160)` 的单轮调用上限。

### 4.3 压缩后“下文如何使用上文”

这是本方案的核心语义。假设消息状态为：

```
M0 用户目标
M1 AI  -> tool_calls(browser_run_code)
M2 Tool 结果
M3 AI 分析
M4 Tool 结果
...
M40 最近 AI
M41 最近 Tool 结果
```

第一次压缩（cutoff = 30）：

```
state.messages          = [M0 ... M41]                  # 原始账本，不删除
_summarization_event    = {cutoff_index: 30, summary_message: S1, file_path: ...}

本次及后续模型调用看到：
  effective_messages = [S1] + [M30 ... M41]
```

第二次压缩时，`effective_messages` 会作为新的输入：旧摘要 `S1` 与部分尾部消息一起被再次摘要成 `S2`，`cutoff_index` 按绝对下标继续前移。deepagents 的 `_compute_state_cutoff` 已经处理了“有效列表下标 → 状态绝对下标”的换算，**不要自己重新计算**。

因此：

- 同一轮内的后续模型调用：自动使用最新摘要；
- 下一轮用户消息：checkpointer 恢复状态，`awrap_model_call` 仍按 `_summarization_event` 重建；
- 后端重启：checkpointer 中事件仍在，行为一致；
- 若会话没有 checkpointer 历史、从 Mongo 注入历史（`runner._load_history`）：第一次模型调用时照常判定并压缩。

### 4.4 为什么不直接使用默认的 `SummarizationMiddleware`

| 方案 | 优点 | 问题 |
| --- | --- | --- |
| 保持默认 | 零改动 | 阈值 170k / 6 条保留；不可观测；不可手动触发；中文 token 低估 |
| 自己实现完整压缩 | 完全可控 | 要重做 offload、工具配对、溢出重试、二次压缩下标，重复且高风险 |
| **推荐：替换默认实例 + 薄封装** | 复用成熟逻辑，补上策略/事件/审计 | 少量依赖 deepagents 私有钩子，需要版本锁定与回归测试 |

推荐做法：在 `build_agent` 的 `middleware=[...]` 中放一个**同名**（`name == "SummarizationMiddleware"`）的中间件。deepagents 的 `_apply_custom_middleware`（`graph.py:211-248`）会按名字**原位替换**默认实例，不会产生两个压缩器：

```python
# backend/services/agent/agent.py
from .core.compression import build_compression_middleware

return create_deep_agent(
    model=model,
    tools=tools,
    system_prompt=SYSTEM_PROMPT,
    middleware=[
        AgentEventMiddleware(session),
        # 与 deepagents 内置实现同名, 原位替换其默认压缩器
        build_compression_middleware(cfg, session, model),
        TodoListMiddleware(system_prompt=""),
        ModelCallLimitMiddleware(...),
    ],
    ...
)
```

> 注意：如果只是 `SummarizationMiddleware(...)` 实例而不改 `name`，deepagents 会把它当成“新增中间件”追加，默认压缩器仍然存在，最终两个压缩器同时工作。**必须确保 `name` 为 `"SummarizationMiddleware"`**。

---

## 5. 何时压缩（判定策略）

### 5.1 预算公式

```
usable_tokens   = context_window - reserve_output_tokens - safety_margin
trigger_tokens  = min(floor(context_window * trigger_fraction), usable_tokens)
hard_tokens     = floor(context_window * hard_fraction)
```

建议默认值：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `context_window` | `128000` | 按实际模型设置；DeepSeek 兼容模型没有 profile 时必须显式配置 |
| `trigger_fraction` | `0.62` | 约 79k 触发，给当前任务留足空间 |
| `hard_fraction` | `0.85` | 超过则忽略防抖，立即压缩 |
| `reserve_output_tokens` | `8192` | 给当前回复/工具调用留出输出预算 |
| `safety_margin` | `4096` | 估算误差与工具 schema 变化 |
| `trigger_messages` | `90` | 消息条数兜底（防止大量短消息） |
| `keep_messages` | `12` | 压缩后保留的最近消息数 |
| `min_history_tokens` | `8000` | 可压缩部分太小时不压，避免浪费一次摘要调用 |
| `min_gap_messages` | `6` | 两次压缩之间至少新增的消息数 |
| `min_gap_tokens` | `6000` | 两次压缩之间至少新增的 token 数 |
| `summary_max_tokens` | `1200` | 摘要输出预算（约 3000~4000 中文字符） |
| `trim_tokens_to_summarize` | `60000` | 送给摘要模型的历史上限，防止摘要请求本身超窗 |
| `summary_timeout_s` | `90` | 摘要调用超时 |

`context_window` 必须可配置：`--context-window` / `CONTEXT_WINDOW`。原因是 `ChatOpenAI` 对自建网关/DeepSeek 通常拿不到 `model.profile`，默认 170k 阈值不可信。

### 5.2 token 估算

使用 `langchain_core.messages.utils.count_tokens_approximately`，但要修正中文与图片：

```python
from functools import partial
from langchain_core.messages.utils import count_tokens_approximately

token_counter = partial(
    count_tokens_approximately,
    chars_per_token=1.8,          # 中文为主：1 token ≈ 1.5~2 字符
    tokens_per_image=900,         # 1280px 截图的保守估计（默认 85 太低）
    use_usage_metadata_scaling=True,
    extra_tokens_per_message=3.0,
)
```

- 估算范围必须包含 **system prompt + tool schemas + messages**，与 deepagents 的 `_count_tokens` 保持一致；
- `use_usage_metadata_scaling=True` 会利用最近一次 AI 回复的 `usage_metadata.total_tokens` 校准比例；
- 中文比例可再通过配置 `--compress-chars-per-token` 调整；
- 估算只用于“触发判定”，不要求精确；宁可略高估。

### 5.3 触发条件

一次压缩需要同时满足：

```
(1) total_tokens >= trigger_tokens
    或 message_count >= trigger_messages
    或 total_tokens >= hard_tokens          # 硬阈值，忽略防抖

(2) 可压缩历史足够：
    old_messages >= keep_messages + 2
    且 old_tokens >= min_history_tokens

(3) 防抖通过（非硬阈值时）：
    messages_since_last_compression >= min_gap_messages
    且 tokens_since_last_compression >= min_gap_tokens
```

补充规则：

- **本轮第一条消息不压**：如果 `messages` 太少（例如首轮刚开始），即使 system prompt 很大也不压；
- **用户明确要求“从头开始/忘记之前”时**，走手动压缩，不依赖阈值；
- **工具调用批次中间不主动切**：由 cutoff 安全规则保证（见 5.4）。

### 5.4 保留窗口与切割点

deepagents 的 `_find_safe_cutoff_point` 已经做了工具配对保护：如果切割点落在 `ToolMessage` 上，会向前回退到对应的 `AIMessage(tool_calls)`，把整批调用与结果一起留下。除此之外，本方案再加三条约束：

1. **切割点不能落在最后一批工具调用内部**：保留窗口至少包含最近一次完整的 `AI(tool_calls) + ToolMessage(s)`。
2. **保留最近一条用户消息**：如果它在保留窗口之外，摘要 prompt 必须逐字带上它的开头部分（最多 500 字），并在摘要的「任务目标」中置顶，保证模型始终知道“当前要做什么”。
3. **保留当前 plan/todos 快照**：它们不在 `messages` 里，但会随摘要一起显式写入，避免旧消息被压掉后丢失进度约束。

保留窗口默认按“最近 12 条消息”，而不是按 token 比例。原因是爬虫任务的消息长度差异极大（一条 `page_analyze` 可能几千字符，一条状态只有几十字符），按条数更容易保证“最近几次工具交互”完整。若保留窗口本身超过预算的 35%，则把 `keep_messages` 临时降到 6 再压一次；仍超则进入 L4 硬截断。

---

## 6. 压缩内容规范（摘要 Prompt）

摘要是整个方案质量的上限。默认的英文通用摘要 prompt 不足以支撑爬虫任务，必须用**结构化、任务导向**的中文 prompt（放在 `backend/services/agent/prompts/compress.md`，由 `prompts/__init__.py` 加载）。

### 6.1 摘要必须包含的字段

```markdown
## 任务目标
- 用户最初要什么、当前轮次要什么（逐字保留关键 URL / 站点名 / 数据字段）

## 当前计划与进度
- plan.goal / method / login_required / data_fields
- 每个 step 的当前状态（completed / in_progress / pending）
- 与 plan 对应的 todo 状态快照

## 已确认的事实
- 目标站点结构：静态/动态、列表页/详情页、分页方式
- 已验证可用的数据接口：方法、URL、关键参数、返回字段
- 已验证可用的选择器（逐字保留 CSS/XPath）
- 反爬/风控特征、验证类型、已采取的措施
- 登录状态：是否需要登录、登录方式、凭据是否已保存（**不写凭据值**）

## 已做出的决策与原因
- 为什么选 httpx / Playwright / 浏览器渲染
- 为什么放弃某个入口或方案

## 失败与排除项
- 报错信息的关键行、已尝试但失败的方案、不要重复尝试的操作

## 产物与文件
- 已保存的数据路径（/saved/...、/agent_saved/...）
- 已归档脚本路径（/agent_backend/...）
- 编辑器脚本当前状态：最后一次 set_editor_code 的版本要点、是否已可运行

## 未解决问题与下一步
- 当前卡点、待验证假设、下一步最小动作
```

### 6.2 摘要的硬性约束

1. **逐字保留标识符**：URL、接口路径、字段名、选择器、错误码、文件路径不能改写或概括。
2. **不写凭据明文**：cookie、token、密码、验证码只描述“已保存/已失效”，不写值。
3. **不复述大段内容**：不贴完整代码、HTML、日志；只保留结论和关键片段（每段不超过 3 行）。
4. **不杜撰**：没有验证过的内容标注“未验证”；不确定的内容标注“待确认”。
5. **保留进度语义**：已完成的步骤不能写成待办；已放弃的方案不能写成待尝试。
6. **控制长度**：目标 ≤ `summary_max_tokens`（默认 1200 token）；超长时优先删“过程细节”，不删“事实/决策/路径”。

### 6.3 摘要消息的注入格式

deepagents 会在摘要前加一段英文包装（`_build_new_messages_with_path`）。建议在子类中覆盖为中文版本，并带上版本号与覆盖范围：

```text
你正在继续一段已被压缩的对话。

被压缩的历史原文保存在 {file_path}，如需细节可用 read_file 查看。

以下是第 {n} 次上下文压缩摘要（覆盖到第 {cutoff} 条消息）：

<summary>
{summary}
</summary>

注意：摘要可能省略过程细节，涉及 URL、选择器、字段名、文件路径时以摘要中的记录为准；
若摘要与最近消息冲突，以最近消息为准。
```

摘要消息使用 `HumanMessage`，`additional_kwargs={"lc_source": "context_compression", "compression_version": n}`，便于后续统计与调试。

---

## 7. 手动压缩

### 7.1 产品入口

在 Agent 会话面板增加“压缩上下文”按钮（仅在会话 `idle` / `done` / `error` 时可用，运行中置灰或提示“将在下一次模型调用前执行”）。

### 7.2 后端接口

```http
POST /api/v1/agent/sessions/{session_id}/compress
```

行为：

1. 校验 `crawler_id` 与会话归属；
2. 若会话正在 `running` / `waiting`：
   - 设置 `session.compress_requested = True`；
   - 返回 `{"ok": true, "deferred": true}`；
   - 下一次 `awrap_model_call` 消费该标记并强制压缩；
3. 若会话空闲：
   - 先尝试通过 `session.agent.aupdate_state` / 下一次用户消息触发；
   - 推荐实现：仍然设置标记，并提示“下次对话生效”，避免在无模型请求时手工拼接图状态；
4. 发送 `context_compression_requested` 事件（瞬时，不落库）。

> 备选：启用 deepagents 的 `SummarizationToolMiddleware`，给模型一个 `compact_conversation` 工具，让 Agent 自己决定何时压缩。产品按钮与工具可以共存，二者共用同一个 `_summarization_event`。

### 7.3 手动压缩的约束

- 可压缩历史不足（`old_messages < keep_messages + 2`）时拒绝，返回 409/400 并说明原因；
- 手动压缩绕过防抖，但不绕过“工具配对安全”和“保留当前用户消息”；
- 手动压缩同样发送开始/完成事件，reason 记为 `manual`。

---

## 8. 事件、状态与可观测性

### 8.1 新增事件

| 事件 | 是否落库 | 说明 |
| --- | --- | --- |
| `context_compression_start` | 否 | 开始压缩，前端显示“正在整理上下文…” |
| `context_compression` | 是（observe 模式下仅实时、不落库） | 压缩完成，前端插入分隔卡片；observe 模式带 `applied=false` |
| `context_compression_error` | 是 | 摘要失败但已降级，前端提示“上下文整理失败，已使用兜底策略” |
| `context_compression_requested` | 否 | 手动压缩请求已受理（可能延迟生效） |

`context_compression` 事件字段：

```json
{
  "type": "context_compression",
  "session_id": "a1b2c3d4",
  "seq": 42,
  "reason": "auto",
  "before_tokens": 91234,
  "after_tokens": 38120,
  "saved_tokens": 53114,
  "message_count_before": 118,
  "message_count_after": 13,
  "summarized_messages": 105,
  "keep_messages": 12,
  "summary_chars": 2860,
  "history_path": "/conversation_history/session_9f2c....md",
  "duration_ms": 4210,
  "version": 1,
  "model": "deepseek-v4-flash"
}
```

说明：

- `before_tokens` / `after_tokens` 都用同一个 token_counter 估算；
- `saved_tokens` 用于前端展示“已释放约 53k tokens”；
- `version` 是会话内压缩次数，从 1 开始；
- `history_path` 为虚拟路径，前端不要直接下载（文件在 `tmp/` 下，受 24h 清理）。

### 8.2 前端改动

`frontend/src/hooks/useAgent.ts`：

- `ACTIVITY_TYPES` 增加 `context_compression` / `context_compression_error`；
- `handleEvent` 增加分支，往 feed 里插入 `{ kind: "compression", ... }`；
- 会话恢复时从 `agent_messages` 中 `role="event", type="context_compression"` 重建卡片（`_persist_event` 已经支持事件落库）；
- 卡片展示：压缩前后 token、释放比例、摘要可展开（默认折叠，避免打断阅读）；
- 运行中显示“上下文压缩中…”的轻量状态，不阻塞流式输出。

`AgentEvent` 接口补充字段：`before_tokens`、`after_tokens`、`saved_tokens`、`reason`、`history_path`、`version`。

### 8.3 日志与指标

- 后端 logger：`[ContextCompression] session=%s reason=%s before=%d after=%d cutoff=%d duration=%.1fms`；
- Session 文档新增字段（`agent_sessions`）：
  - `compression_count`
  - `last_compression_at`
  - `context_tokens_estimate`
  - `last_compression`（事件字段子集）
- 建议指标（Prometheus/日志聚合均可）：
  - 压缩次数 / 会话
  - 压缩成功率（摘要调用成功 / 触发次数）
  - 平均压缩比（`after / before`）
  - 摘要耗时 P50/P95
  - 溢出自愈次数（`ContextOverflowError` 后恢复成功）

---

## 9. 数据持久化与恢复

### 9.1 Checkpoint 内

复用 deepagents 的私有状态通道：

```python
_summarization_event = {
    "cutoff_index": 105,                 # 状态消息的绝对下标
    "summary_message": HumanMessage(...),
    "file_path": "/conversation_history/session_xxx.md",
}
_summarization_session_id = "session_xxx"
```

- 由 `ExtendedModelResponse(command=Command(update=...))` 写入；
- 随 checkpointer 落 MongoDB，**跨轮次、跨重启生效**；
- 原始 `messages` 不删除，UI 与复盘不受影响。

### 9.2 Mongo 审计集合

新增 `agent_compactions`（`backend/services/agent/session/store.py` 增加方法）：

```json
{
  "session_id": "a1b2c3d4",
  "crawler_id": "dev_test",
  "version": 1,
  "reason": "auto",
  "ts": 1760000000.0,
  "before_tokens": 91234,
  "after_tokens": 38120,
  "summarized_messages": 105,
  "keep_messages": 12,
  "summary": "……",
  "history_path": "/conversation_history/session_xxx.md",
  "model": "deepseek-v4-flash",
  "duration_ms": 4210
}
```

索引：

```python
await db["agent_compactions"].create_index([("session_id", 1), ("ts", -1)])
await db["agent_compactions"].create_index([("crawler_id", 1), ("ts", -1)])
```

删除会话时同步删除审计（`delete_session` 中增加一次 `delete_many`），避免孤儿数据。

> `version` 以 `agent_sessions.compression_count` 为准：会话加载/重启恢复时把它读回
> `AgentSession`，不要只依赖进程内计数，否则后端重启后版本号会从 1 重新开始。

### 9.3 重启与异常恢复

| 场景 | 行为 |
| --- | --- |
| 后端重启 | checkpointer 恢复 `_summarization_event`，下次模型调用直接使用摘要视图 |
| 事件下标越界（消息被外部修改） | deepagents 会退化为“仅摘要消息”（`cutoff > len` 时丢掉尾部）；本方案在 `awrap_model_call` 外层检测到异常后清空该事件并重新压缩 |
| 摘要消息缺失/损坏 | 视为无压缩事件，重新按阈值压缩 |
| 删除会话 | `adelete_thread` 删除 checkpoint；`agent_compactions` 同步删除；`conversation_history` 文件由 `tmp_cleanup` 24h 清理 |
| 旧会话（无压缩事件） | 首次超过阈值时正常压缩，无需迁移 |

---

## 10. 失败与降级

| 失败点 | 处理 |
| --- | --- |
| 摘要模型超时/429/5xx | 重试 2 次（指数退避）；仍失败 → 结构化兜底摘要 + `context_compression_error` 事件，继续任务 |
| 摘要输出为空/格式严重不符 | 重试 1 次；仍失败 → 使用“确定性摘要”（目标 + plan/todos 快照 + 最近文件路径） |
| 摘要请求本身超窗 | 基础中间件在调用 `summarizer` 前会按 `trim_tokens_to_summarize` 裁剪历史；自定义 summarizer 自己捕获 `ContextOverflowError`，把内部预算减半后重试一次 |
| 压缩后仍 `ContextOverflowError` | 触发 L4：保留首条用户目标 + 最新摘要 + 最近 6 条消息；仍有问题则只保留最近 3 条工具结果并重试一次 |
| `cutoff_index <= 0`（没有可压部分） | 不压缩，正常调用；若仍超窗，走 L4；手动压缩时发 `context_compression_error` 说明“可压缩历史不足” |
| 压缩发生在 `ask_user` 挂起期间 | 不会发生：挂起时没有模型调用；用户回答后下一次模型调用前照常判定 |
| 压缩发生在工具调用之间 | 不会切开批次：`_find_safe_cutoff_point` 保证配对；保留窗口再兜底 |
| 摘要调用消耗额外 token | 有 `min_gap_*` 防抖 + `min_history_tokens` 下限；摘要模型可选更便宜的模型 |
| 并发压缩 | 全局单 Agent 运行锁保证同一时刻只有一个轮次；中间件内部再加 `asyncio.Lock` 防止重入 |

---

## 11. 配置与代码改造清单

### 11.1 配置项（`backend/config.py`）

```python
# ---- 上下文压缩 ----
context_compress: bool = True              # 总开关
context_window: int = 128_000              # 模型上下文窗口
compress_trigger_fraction: float = 0.62    # 触发比例
compress_hard_fraction: float = 0.85       # 硬触发比例
compress_keep_messages: int = 12           # 保留窗口消息数
compress_trigger_messages: int = 90        # 消息条数触发
compress_min_history_tokens: int = 8_000   # 最小可压缩 token
compress_min_gap_messages: int = 6         # 防抖：最小新增消息数
compress_min_gap_tokens: int = 6_000       # 防抖：最小新增 token
compress_summary_max_tokens: int = 1_200   # 摘要输出预算
compress_trim_tokens: int = 60_000         # 摘要输入上限
compress_timeout: float = 90.0             # 摘要超时(秒)
compress_model: str = ""                   # 可选：独立摘要模型(复用主模型 provider/base_url/api_key)
compress_chars_per_token: float = 1.8      # 中文 token 估算
compress_mode: str = "auto"                # auto / observe / off
```

CLI / 环境变量：

```bash
--context-window 128000            CONTEXT_WINDOW=128000
--compress-trigger-fraction 0.62   COMPRESS_TRIGGER_FRACTION=0.62
--compress-keep-messages 12        COMPRESS_KEEP_MESSAGES=12
--compress-model deepseek-chat     COMPRESS_MODEL=deepseek-chat
--no-context-compress              CONTEXT_COMPRESS=0
--compress-mode observe            COMPRESS_MODE=observe
```

`observe` 模式只计算并上报“将压缩 / 可释放 token”，不修改上下文，用于灰度观察。

### 11.2 新增文件

```
backend/services/agent/core/compression.py      # 策略、token 估算、中间件、摘要回调
backend/services/agent/prompts/compress.md      # 结构化摘要 prompt
```

`compression.py` 职责：

```python
"""Agent 上下文压缩: 阈值判定、摘要生成、事件与审计。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Sequence

from deepagents.middleware.summarization import (
    SUMMARIZATION_EVENT_KEY,
    SummarizationMiddleware,
)
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AnyMessage, SystemMessage, get_buffer_string
from langchain_core.messages.utils import count_tokens_approximately

from .llm import build_chat_model
from .text import content_text


def estimate_request_tokens(request: ModelRequest, policy: CompressionPolicy) -> int:
    """估算一次模型请求: system prompt + tool schema + 消息。"""
    messages = list(request.messages)
    if request.system_message is not None:
        messages = [request.system_message, *messages]
    return policy.token_counter()(messages, tools=request.tools)


def estimate_after_tokens(request: ModelRequest, event: dict, policy: CompressionPolicy) -> int:
    """估算压缩后模型实际看到的上下文(摘要 + 保留窗口)。"""
    messages = [event["summary_message"], *request.messages[event["cutoff_index"]:]]
    if request.system_message is not None:
        messages = [request.system_message, *messages]
    return policy.token_counter()(messages, tools=request.tools)


@dataclass
class CompressionPolicy:
    """压缩策略(全部来自 Config, 便于测试直接构造)。"""
    context_window: int = 128_000
    trigger_fraction: float = 0.62
    hard_fraction: float = 0.85
    keep_messages: int = 12
    trigger_messages: int = 90
    min_history_tokens: int = 8_000
    min_gap_messages: int = 6
    min_gap_tokens: int = 6_000
    summary_max_tokens: int = 1_200
    trim_tokens: int = 60_000
    timeout: float = 90.0
    chars_per_token: float = 1.8
    mode: str = "auto"                 # auto / observe / off

    @property
    def trigger_tokens(self) -> int:
        usable = self.context_window - 8192 - 4096
        return min(int(self.context_window * self.trigger_fraction), usable)

    @property
    def hard_tokens(self) -> int:
        return int(self.context_window * self.hard_fraction)

    def token_counter(self):
        return partial(
            count_tokens_approximately,
            chars_per_token=self.chars_per_token,
            tokens_per_image=900,
            use_usage_metadata_scaling=True,
        )


class SessionCompressionMiddleware(SummarizationMiddleware):
    """在 deepagents 压缩器上补充: 项目阈值、中文摘要、事件、审计、手动触发。

    关键点: name 必须与 deepagents 内置实现同名, 否则会被当成新增中间件,
    导致两个压缩器同时工作。
    """

    @property
    def name(self) -> str:
        return "SummarizationMiddleware"

    def __init__(self, cfg, session, model, backend, policy: CompressionPolicy,
                 audit=None):
        self._session = session
        self._cfg = cfg
        self._policy = policy
        self._audit = audit              # runner 注入: async (payload, event) -> None
        self._main_model = model
        self._inflight = asyncio.Lock()
        self._force_next = False
        self._before_tokens = 0
        super().__init__(
            model=model,
            backend=backend,
            trigger=list(self._trigger_specs()),
            keep=("messages", policy.keep_messages),
            token_counter=policy.token_counter(),
            trim_tokens_to_summarize=policy.trim_tokens,
            truncate_args_settings={
                "trigger": ("messages", 20),
                "keep": ("messages", policy.keep_messages),
                "max_length": 2000,
                "truncation_text": "\n...[旧工具参数已截断, 完整内容见对应工具结果/文件]",
            },
            # 传入自定义 summarizer 后, summary_prompt 不再生效;
            # 摘要 prompt 由 _summarize 自己组装(见 6.3 / 附录 A)。
            summarizer=self._summarize,
        )

    def _trigger_specs(self):
        # 列表 = OR: token 到阈值 或 消息条数到阈值
        return [
            ("tokens", self._policy.trigger_tokens),
            ("messages", self._policy.trigger_messages),
        ]

    def _should_summarize(self, messages, total_tokens):
        # 手动压缩: 一次性强制触发(唯一依赖的私有钩子, 见 11.3)
        if self._force_next and len(messages) > self._policy.keep_messages + 2:
            self._force_next = False
            return True
        return super()._should_summarize(messages, total_tokens)

    async def awrap_model_call(self, request: ModelRequest, handler):
        self._before_tokens = estimate_request_tokens(request, self._policy)
        forced = self._force_next          # 本次是否由手动触发
        started = time.monotonic()
        response = await super().awrap_model_call(request, handler)
        # 压缩是否发生: 看中间件是否返回了 _summarization_event 状态更新
        update = getattr(getattr(response, "command", None), "update", None) or {}
        event = update.get(SUMMARIZATION_EVENT_KEY)
        if event:
            await self._on_compressed(request, event, started, forced)
        return response

    def _resolve_summary_model(self):
        """摘要模型: 配置了独立模型时用独立模型, 否则复用主模型。"""
        if self._cfg.compress_model:
            return build_chat_model(
                replace(self._cfg, llm_model=self._cfg.compress_model)
            )
        return self._main_model

    async def _summarize(self, messages: Sequence[AnyMessage]) -> str:
        # 摘要调用前发开始事件; prompt/model 在这里完全可控,
        # 便于使用独立摘要模型、中文模板与超时控制。
        self._session.emit({
            "type": "context_compression_start",
            "before_tokens": self._before_tokens,
            "summarized_messages": len(messages),
        })
        model = self._resolve_summary_model()
        prompt = load_compress_prompt().format(
            messages=get_buffer_string(messages, format="xml")
        )
        resp = await asyncio.wait_for(
            model.ainvoke(prompt),
            timeout=self._policy.timeout,
        )
        return _cap_summary(content_text(resp.content), self._policy.summary_max_tokens)

    async def _on_compressed(self, request, event, started, forced):
        after = estimate_after_tokens(request, event, self._policy)
        count_before = len(request.messages)                 # 不含 system
        count_after = count_before - event["cutoff_index"] + 1
        version = getattr(self._session, "compression_count", 0) + 1
        self._session.compression_count = version
        payload = {
            "type": "context_compression",
            "reason": "manual" if forced else "auto",
            "before_tokens": self._before_tokens,
            "after_tokens": after,
            "saved_tokens": max(0, self._before_tokens - after),
            "message_count_before": count_before,
            "message_count_after": count_after,
            "summarized_messages": max(0, count_before - count_after),
            "keep_messages": self._policy.keep_messages,
            "summary_chars": len(content_text(event["summary_message"].content)),
            "history_path": event.get("file_path"),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "version": version,
        }
        self._session.emit(payload)                     # 实时广播 + 落库
        if self._audit is not None:
            await self._audit(payload, event)           # agent_compactions
```

> 上述代码是结构示意，用于说明职责边界与调用点；`load_compress_prompt()`（读取
> `prompts/compress.md`）、`_cap_summary()`（按摘要预算截断）、`build_compression_middleware()`
> （把 `Config` 映射为 `CompressionPolicy` 并注入 `audit` 回调）都在同一模块实现。实现时以
> deepagents 的签名与实际返回类型为准，并补齐异常处理（见第 10 节）。

### 11.3 对 deepagents 私有钩子的依赖

本方案刻意只依赖两个点：

1. `SummarizationMiddleware` 的 `name`/构造参数/`summarizer`（公开）；
2. `_should_summarize`（私有）仅用于“手动强制压缩”的一次性开关。

约束：

- 在 `pyproject.toml` 中把 deepagents 收紧到 `deepagents~=0.7.28`（或至少 `<0.8`）；
- 为 `_should_summarize` 增加一个适配测试（构造假 messages，断言强制标记只生效一次）；
- 如果 deepagents 升级改变了私有方法，**手动压缩降级为“只支持 idle 时下一次对话生效”**，自动压缩不受影响（自动路径完全走公开配置）。

### 11.4 其他文件改动

| 文件 | 改动 |
| --- | --- |
| `backend/services/agent/agent.py` | 注入 `build_compression_middleware(...)`，同名替换默认压缩器 |
| `backend/services/agent/core/compression.py` | 新增：策略、中间件、摘要 prompt 组装、审计 |
| `backend/services/agent/prompts/compress.md` | 新增：结构化摘要 prompt |
| `backend/services/agent/session/model.py` | `AgentSession` 增加 `compress_requested: bool`、`compression_count: int`、`last_compression: dict | None` |
| `backend/services/agent/session/store.py` | 新增 `add_compaction` / `list_compactions`，删除会话时清理审计 |
| `backend/services/agent/session/event.py` | `PERSISTED_EVENT_TYPES` 增加 `context_compression`、`context_compression_error` |
| `backend/services/agent/runner.py` | `_persist_event` 处理压缩事件；`delete_session` 清理审计；启动恢复时重置 `compress_requested` |
| `backend/routers/agent.py` | 新增手动压缩接口；schemas 增加请求/响应模型 |
| `backend/config.py` | 新增压缩配置与 CLI 参数 |
| `frontend/src/hooks/useAgent.ts` | 事件处理、`ACTIVITY_TYPES`、恢复历史时的压缩卡片 |
| `frontend/src/components/...` | 会话时间线中的压缩卡片与“压缩上下文”按钮 |
| `tests/test_agent_compression.py` | 新增单元/集成测试（见第 13 节） |
| `tests/test_config.py` | 配置解析与默认值测试 |

---

## 12. 端到端时序

```
用户发送消息
   │
   ▼
runner.send_message ── 落库 user 消息 ── _run_turn
   │
   ▼
agent.astream({messages:[HumanMessage]})
   │
   ├─ 模型节点: SessionCompressionMiddleware.awrap_model_call
   │    │
   │    ├─ effective = [S_prev] + state.messages[cutoff_prev:]
   │    ├─ tokens = count(effective, system, tools)
   │    │
   │    ├─ tokens < trigger ──────────────► 正常调用模型
   │    │
   │    └─ tokens >= trigger
   │         ├─ emit context_compression_start
   │         ├─ offload 旧消息 -> /conversation_history/<id>.md
   │         ├─ 调摘要模型 -> S_new
   │         ├─ 本次调用 messages = [S_new] + 最近 keep 条
   │         ├─ 返回 ExtendedModelResponse(command=update(event))
   │         └─ emit context_compression + 落审计
   │
   ├─ 工具节点执行 browser_run_code / page_analyze / ...
   │    └─ 结果进入 state.messages（原始账本继续增长）
   │
   └─ 下一次模型调用: 重复上述判定（可能再次压缩）
   │
   ▼
本轮结束 ── _finalize_done ── 下一轮/重启仍按事件重建上下文
```

---

## 13. 测试计划

### 13.1 单元测试（`tests/test_agent_compression.py`）

1. **token 估算**
   - 中文文本：`chars_per_token=1.8` 时估算值高于默认 4.0；
   - 图片 content block：按 `tokens_per_image=900` 计，不按 base64 长度计；
   - 工具 schema 计入总数。
2. **策略判定**
   - 低于 token/消息阈值不触发；
   - token 触发与消息触发是 OR；
   - 超过硬阈值忽略防抖；
   - `old_tokens < min_history_tokens` 不触发；
   - `min_gap_*` 防抖生效。
3. **切割安全**
   - 构造 `AI(tool_calls) + ToolMessage + ToolMessage`，断言 cutoff 不落在 ToolMessage 上；
   - 保留窗口包含完整工具批次；
   - 最近用户消息被保留/pin。
4. **摘要**
   - prompt 包含 plan/todos 快照；
   - 摘要超长被裁剪到预算；
   - 凭据值不会出现在摘要中（构造带 cookie 的输入，断言被脱敏/摘要 prompt 明确禁止）。
5. **事件与审计**
   - 压缩完成后发出 `context_compression`，字段齐全、数值自洽（`saved = before - after`）；
   - `agent_compactions` 写入一条记录；
   - `PERSISTED_EVENT_TYPES` 包含压缩事件。
6. **手动触发**
   - `compress_requested=True` 只强制一次；
   - 可压缩历史不足时拒绝；
   - 运行中请求返回 `deferred`。

### 13.2 集成测试（FakeModel / FakeStore / FakeAgent）

沿用 `tests/test_agent_runner.py` 的内存版假对象模式：

1. 构造 60+ 条消息的会话，FakeModel 返回带 `usage_metadata` 的回复，断言：
   - 第一次模型调用前触发压缩；
   - 状态中出现 `_summarization_event`；
   - 第二次模型调用收到的是 `[summary] + tail`，而不是全量；
   - `state.messages` 原始条数不变。
2. 第二轮用户消息：断言继续使用摘要事件，不会重新注入全量历史。
3. 重启模拟：新建 manager / session 从同一 FakeCheckpointer 恢复，断言仍使用摘要。
4. 未超过阈值：断言不触发、不发事件。
5. `ContextOverflowError`：FakeModel 第一次抛错，断言中间件压缩后重试成功。
6. 摘要失败：FakeModel 抛异常，断言发出 `context_compression_error` 且任务继续。
7. `ask_user` 打断后恢复：断言压缩发生在恢复后的下一次模型调用，不会破坏 `question` 状态。
8. 事件落库：断言 `agent_messages` 中出现 `role="event", type="context_compression"`。

### 13.3 回归

- `uv run pytest` 全量通过（现有 ~560 个测试）；
- `uv run pytest --cov=backend --cov-report=term`，新增模块覆盖率 ≥ 90%；
- 手工验证：长任务（>80 次工具调用）压缩后仍能继续完成并写回编辑器脚本；
- 手工验证：压缩后刷新页面，会话历史完整、压缩卡片存在。

---

## 14. 灰度与上线

1. **observe 模式**（1 个版本）：`--compress-mode observe`
   - 只计算“是否将触发、可释放 token”，发 `context_compression` 事件（`applied=false`，仅实时、不落库）；
   - 观察真实会话的 token 分布，校准 `context_window` / `trigger_fraction` / `keep_messages`。
2. **auto 模式，保守参数**：
   - 先只对“消息数 > 100 或 token > 80k”的会话启用；
   - 观察压缩后任务完成率、摘要质量投诉、平均轮次耗时。
3. **默认开启**：
   - 在 `Config` 中默认 `context_compress=True`；
   - 保留 `--no-context-compress` 应急关闭。
4. **回滚**：
   - 关闭开关即可停止新增压缩；
   - `_summarization_event` 已存在时，不能简单移除中间件：默认压缩器仍会读取该事件，且移除后事件语义会失效。安全的回滚方式是**保留中间件、把阈值调到不可达**（`trigger=None` / 极大值），让已有摘要继续生效、但不再产生新压缩；
   - 如需彻底回到“全量上下文”，需要同时清空 checkpoint 中的 `_summarization_event`（不推荐在线操作）。

---

## 15. 风险与权衡

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 摘要丢失关键选择器/接口 | 后续步骤重复试错 | 结构化 prompt 强制逐字保留标识符；保留最近 12 条；原文 offload 可回查；用真实长会话做 golden 评测 |
| 中文 token 低估导致触发过晚 | 仍然超窗 | 默认 `chars_per_token=1.8`；`use_usage_metadata_scaling`；硬阈值 0.85 + `ContextOverflowError` 兜底 |
| 摘要调用本身超窗 | 压缩失败 | `trim_tokens_to_summarize=60k`；异常时减半重试；确定性兜底摘要 |
| 压缩过于频繁 | 增加费用/延迟 | `min_gap_messages` / `min_gap_tokens`；`min_history_tokens`；observe 模式校准 |
| 依赖 deepagents 私有钩子 | 升级破坏手动压缩 | 版本收紧 + 适配测试；手动压缩可降级，自动路径只用公开配置 |
| 压缩后模型“忘记”当前轮目标 | 答非所问 | 保留最近用户消息；摘要中置顶“当前轮次要什么”；压缩后第一条消息显式提醒 |
| 与 `TodoListMiddleware` 状态不一致 | 进度展示错乱 | 摘要在 prompt 中携带 plan/todos 快照；`todos` 本身仍在独立 state channel，不受压缩影响 |
| offload 文件被清理 | 无法回查旧细节 | 摘要已包含关键事实；`tmp_cleanup` 保留 24h；重要产物应写 `/agent_saved` 或用 `set_editor_code` 固化 |
| 多模态图片 token 估算偏差 | 触发偏晚 | `tokens_per_image=900` 保守估计；旧图片在摘要前被 offload 成路径引用 |

---

## 16. 验收标准

- [ ] 长会话（> 100 条消息或 > 80k token）自动触发一次压缩，模型调用 token 下降 ≥ 50%，任务继续完成。
- [ ] 压缩后同一轮、下一轮、后端重启后，模型都只看到“摘要 + 保留窗口”。
- [ ] 原始 `state.messages` 与前端会话历史不被删除，UI 时间线出现压缩卡片。
- [ ] `AIMessage(tool_calls)` 与 `ToolMessage` 永远不会被切开。
- [ ] 摘要包含任务目标、plan/todos、可用接口/选择器、失败排除项、产物路径、下一步。
- [ ] 摘要在 1200 token 内，凭据值不落入摘要与审计。
- [ ] 手动压缩可用；运行中请求会延迟到下一次模型调用。
- [ ] 摘要失败/超窗/压缩后仍超窗时，任务不中断，发出降级事件。
- [ ] `--no-context-compress` 可关闭；observe 模式可只观测不改写。
- [ ] 新增测试通过，全量 `uv run pytest` 通过，新增模块覆盖率 ≥ 90%。

---

## 附录 A：结构化摘要 Prompt（`prompts/compress.md`）

```markdown
你是一个爬虫任务上下文的压缩器。请把「旧对话」压缩成一份供后续继续工作的结构化摘要。

要求：
1. 只保留对继续完成任务有用的信息；删除寒暄、重复解释、冗长的日志与代码。
2. URL、接口路径、HTTP 方法、参数名、返回字段、CSS/XPath 选择器、错误码、文件路径必须逐字保留。
3. 不写任何凭据明文（cookie / token / password / 验证码）；只描述“已保存 / 已失效 / 未获取”。
4. 不杜撰未验证的信息；不确定的内容标注“待确认”。
5. 已完成的步骤不得写成待办；已放弃的方案必须标注“不要重试”。
6. 总长度控制在约 1200 token（或配置的摘要预算）以内；超长时优先删除过程细节，保留事实、决策、路径、下一步。
7. 输出严格的 Markdown，不要额外解释。

请按以下结构输出：

## 任务目标
（用户最初要什么；当前轮次要什么；关键 URL / 站点 / 数据字段）

## 当前计划与进度
（plan.goal / method / login_required / data_fields；每个 step 的状态；
 与 plan 对应的 todo 快照）

## 已确认的事实
（页面结构；已验证接口；已验证选择器；反爬/验证特征；登录状态）

## 已做出的决策与原因
（技术选型；放弃的方案及原因）

## 失败与排除项
（关键报错；已尝试但失败的方案；不要重复的操作）

## 产物与文件
（已保存数据路径；脚本归档路径；编辑器脚本状态与版本要点）

## 未解决问题与下一步
（当前卡点；待验证假设；下一步最小动作）

旧对话如下：

<messages>
{messages}
</messages>
```

> 提示：该模板由 `str.format(messages=...)` 渲染，正文中如出现其它 `{` / `}` 必须写成
> `{{` / `}}`；`{messages}` 是唯一占位符，不要改名。

## 附录 B：压缩事件示例

```json
{
  "type": "context_compression",
  "session_id": "a1b2c3d4e5f6",
  "crawler_id": "dev_test",
  "seq": 128,
  "reason": "auto",
  "before_tokens": 91234,
  "after_tokens": 38120,
  "saved_tokens": 53114,
  "message_count_before": 118,
  "message_count_after": 13,
  "summarized_messages": 105,
  "keep_messages": 12,
  "summary_chars": 2860,
  "history_path": "/conversation_history/session_9f2c1a.md",
  "duration_ms": 4210,
  "version": 1,
  "model": "deepseek-v4-flash"
}
```

## 附录 C：配置示例

```bash
# 128k 窗口，62% 触发，保留最近 12 条消息，使用更便宜的模型做摘要
uv run python -m backend.main \
  --llm-model deepseek-v4-flash \
  --context-window 128000 \
  --compress-trigger-fraction 0.62 \
  --compress-keep-messages 12 \
  --compress-model deepseek-chat

# 只观测不压缩（灰度）
uv run python -m backend.main --compress-mode observe

# 关闭压缩
uv run python -m backend.main --no-context-compress
```

## 附录 D：与现有代码的对应关系

| 关注点 | 现有位置 | 本方案 |
| --- | --- | --- |
| Agent 组装 | `backend/services/agent/agent.py:28-62` | 注入同名压缩中间件 |
| 模型调用钩子 | `backend/services/agent/middleware.py:160-173` | 压缩中间件独立于事件中间件，二者可叠加 |
| 会话状态 | `backend/services/agent/session/model.py:14-48` | 增加压缩相关字段 |
| 检查点 | `backend/services/agent/checkpointer.py` | 复用，不修改 |
| 事件总线 | `backend/services/agent/session/event.py` | 增加持久化事件类型 |
| 会话/消息存储 | `backend/services/agent/session/store.py` | 新增 `agent_compactions` |
| 运行循环 | `backend/services/agent/runner.py:665-980` | 事件落库、删除清理、重启恢复 |
| 前端事件 | `frontend/src/hooks/useAgent.ts:31-160, 820-870` | 压缩卡片与按钮 |
| 工具结果大小 | `backend/services/agent/tools/*.py` | L0 已存在；L1 截断旧参数 |
| 默认压缩器 | `deepagents/graph.py:918`、`deepagents/middleware/summarization.py:1811` | 同名替换 + 自定义策略/摘要/事件 |
