import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import type { AgentFeedItem, AgentLoginRequest, AgentPlan, AgentQuestion, AgentStatus } from "../types";
import { useAgent } from "../hooks/useAgent";
import { useI18n, t } from "../i18n";
import type { MessageKey } from "../i18n";
import AgentDiffCard from "./AgentDiffCard";

const STATUS_TEXT: Record<AgentStatus, MessageKey> = {
  idle: "agent.status.idle",
  running: "agent.status.running",
  waiting: "agent.status.waiting",
  done: "agent.status.done",
  error: "agent.status.error",
  cancelled: "agent.status.cancelled",
};

function TodoBadge({ status }: { status: "pending" | "in_progress" | "completed" }) {
  const mark = status === "completed" ? "✓" : status === "in_progress" ? "•" : " ";
  return <span className={`oc-todo-mark ${status}`}>{`[${mark}]`}</span>;
}

/* ---------------- opencode 风格工具渲染 ---------------- */

function parseArgs(args?: string): Record<string, unknown> | null {
  if (!args) return null;
  try {
    const v: unknown = JSON.parse(args);
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

const str = (v: unknown) => (typeof v === "string" ? v : v === undefined || v === null ? "" : String(v));
const firstLine = (s: unknown) => str(s).split("\n").find((l) => l.trim()) ?? "";
const truncate = (s: string, n: number) => (Array.from(s).length > n ? `${Array.from(s).slice(0, n).join("")}…` : s);
// 会话标题最多展示 15 个字, 超出用省略符, hover 显示全名
const MAX_TITLE_CHARS = 15;
const displayTitle = (s: string) => truncate(s, MAX_TITLE_CHARS);
const codeSummary = (args: Record<string, unknown> | null, lang: string) => {
  const line = firstLine(args?.code).trim();
  return `$ ${lang}  ${truncate(line, 60)}${args?.code && str(args.code).trim().split("\n").length > 1 ? " …" : ""}`;
};

interface ToolSpec {
  icon: string;
  block?: boolean;
  /** 块工具: 是否在内容区展示完整入参代码(如 browser_run_code 的 code) */
  showCode?: boolean;
  summary: (args: Record<string, unknown> | null, item: AgentFeedItem) => string;
  meta?: (args: Record<string, unknown> | null) => string;
}

const TOOL_SPECS: Record<string, ToolSpec> = {
  browser_navigate: {
    icon: "→",
    summary: (a) => `Navigate ${truncate(str(a?.url), 50)}`,
    meta: (a) => (a?.new_page ? "new page" : ""),
  },
  browser_pages: { icon: "⇋", summary: () => "Pages" },
  browser_evaluate: {
    icon: "%",
    summary: (a) => `Evaluate ${truncate(firstLine(a?.expression), 40)}`,
  },
  page_analyze: { icon: "✱", summary: () => "Analyze page" },
  browser_run_code: {
    icon: "$",
    block: true,
    showCode: true,
    summary: (a) => codeSummary(a, "python"),
  },
  debug_code: {
    icon: "$",
    block: true,
    showCode: true,
    summary: (a) => codeSummary(a, "python"),
  },
  get_editor_code: { icon: "→", summary: () => t("agent.tool.readEditorCode") },
  set_editor_code: { icon: "←", summary: () => t("agent.tool.editEditor") },
  http_request: {
    icon: "%",
    summary: (a) => `${str(a?.method || "GET")} ${truncate(str(a?.url), 50)}`,
  },
  api_request: {
    icon: "%",
    summary: (a) => `${str(a?.method || "GET")} ${truncate(str(a?.url), 50)}`,
    meta: (a) => (a?.json_body ? "json" : a?.form && str(a.form) !== "{}" ? "form" : ""),
  },
  record_plan: { icon: "✓", summary: () => t("agent.tool.recordPlan") },
  ask_user: { icon: "→", summary: () => t("agent.tool.askUser") },
  page_login: { icon: "🔐", summary: () => t("agent.tool.pageLogin") },
  archive_content: {
    icon: "⇩",
    summary: (a) => `${t("agent.tool.archive")}${a?.fmt ? ` (${str(a.fmt)})` : ""}`,
  },
};

const toolSpec = (item: AgentFeedItem): ToolSpec => {
  const base = TOOL_SPECS[item.name ?? ""];
  if (base) return base;
  return {
    icon: "⚙",
    summary: (a, it) => {
      if (!a) return it.name ?? "";
      const text = Object.entries(a)
        .map(([k, v]) => `${k}=${str(v)}`)
        .join(", ");
      return `${it.name ?? ""} ${truncate(text.replace(/\s+/g, " ").trim(), 50)}`.trim();
    },
  };
};

function ToolItem({ item }: { item: AgentFeedItem }) {
  const spec = toolSpec(item);
  // 执行代码的块工具(browser_run_code / debug_code)默认缩起代码框, 用户点击展开
  const [open, setOpen] = useState(!spec.block);
  const running = !item.state || item.state === "running";
  const isErr = item.state === "error";
  const done = item.state === "done";
  const args = parseArgs(item.args);
  const summary = spec.summary(args, item);
  const toggle = () => setOpen((o) => !o);

  if (spec.block) {
    return (
      <div className={`oc-block${running ? " running" : ""}${isErr ? " err" : ""}`}>
        <div
          className="oc-block-head"
          role="button"
          tabIndex={0}
          onClick={toggle}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              toggle();
            }
          }}
        >
          {running ? <span className="oc-spin" /> : <span className="oc-block-ico">{spec.icon}</span>}
          <span className="oc-block-title">{summary}</span>
          <span className={`oc-block-state${isErr ? " err" : ""}`}>
            {running ? "…" : isErr ? "✗" : "✓"}
          </span>
          <span className="oc-caret">{open ? "▾" : "▸"}</span>
        </div>
        {open ? (
          <div className="oc-block-body">
            {spec.showCode && args?.code ? <pre className="oc-shell-cmd">{String(args.code)}</pre> : null}
            {done && item.content ? <pre className="oc-shell-out">{item.content}</pre> : null}
            {isErr ? <pre className="oc-shell-out err">{item.error}</pre> : null}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div className={`oc-tool${running ? " running" : ""}${isErr ? " err" : ""}`}>
      <div
        className="oc-tool-head"
        role="button"
        tabIndex={0}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
          }
        }}
      >
        <span className="oc-tool-ico">
          {running ? <span className="oc-spin" /> : <span className="oc-tool-icon">{spec.icon}</span>}
        </span>
        <span className="oc-tool-text">{summary}</span>
        {spec.meta ? <span className="oc-tool-meta">{spec.meta(args)}</span> : null}
        <span className="oc-tool-state">
          {running ? "…" : isErr ? "✗" : "✓"}
        </span>
        <span className="oc-caret">{open ? "▾" : "▸"}</span>
      </div>
      {open ? (
        <div className="oc-tool-detail">
          {item.args ? <pre className="oc-tool-args">{item.args}</pre> : null}
          {done && item.content ? <pre className="oc-tool-out">{item.content}</pre> : null}
          {isErr ? <pre className="oc-tool-out err">{item.error}</pre> : null}
        </div>
      ) : null}
    </div>
  );
}

function SavedItem({ item }: { item: AgentFeedItem }) {
  const [open, setOpen] = useState(true);
  const items = item.saved ?? [];
  return (
    <div className="oc-tool">
      <div
        className="oc-tool-head"
        role="button"
        tabIndex={0}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen((o) => !o);
          }
        }}
      >
        <span className="oc-tool-ico"><span className="oc-tool-icon">⇩</span></span>
        <span className="oc-tool-text">{item.content}</span>
        <span className="oc-tool-state">✓</span>
        <span className="oc-caret">{open ? "▾" : "▸"}</span>
      </div>
      {open ? (
        <div className="oc-tool-detail">
          <ul className="oc-saved-list">
            {items.map((s, i) => (
              <li key={i} className="oc-saved-item">
                <span className={`oc-saved-kind ${s.kind ?? ""}`}>{s.kind ?? "file"}</span>
                <span className="oc-saved-path">{s.path}</span>
                <span className="oc-saved-size">{s.size ?? 0} B</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function FeedItem({ item }: { item: AgentFeedItem }) {
  if (item.kind === "user") {
    return <div className="oc-user">{item.content}</div>;
  }
  if (item.kind === "status") {
    return <div className="oc-status">{item.content}</div>;
  }
  if (item.kind === "message") {
    return <div className="oc-message">{item.content}</div>;
  }
  if (item.kind === "tool") {
    return <ToolItem item={item} />;
  }
  if (item.kind === "saved") {
    return <SavedItem item={item} />;
  }
  if (item.kind === "diff") {
    return (
      <AgentDiffCard
        from={item.diff?.from ?? ""}
        to={item.diff?.to ?? ""}
      />
    );
  }
  if (item.kind === "plan") {
    return <PlanCard plan={item.plan ?? null} />;
  }
  if (item.kind === "answer") {
    return <AnswerCard item={item} />;
  }
  if (item.kind === "error") {
    return <div className="oc-message err">{item.content}</div>;
  }
  return null;
}

function PlanBody({ plan }: { plan: AgentPlan | null }) {
  const { t } = useI18n();
  const kv = (label: string, val: unknown) =>
    val === undefined || val === null || val === "" || (Array.isArray(val) && val.length === 0)
      ? null
      : (
          <div className="plan-row">
            <span className="plan-label">{label}</span>
            <span className="plan-val">
              {Array.isArray(val) ? val.join("、") : String(val)}
            </span>
          </div>
        );
  const rawSteps = Array.isArray(plan?.steps) ? plan.steps : [];
  const steps = rawSteps.map((s) => {
    if (typeof s === "string") return { content: s, status: "pending" as const };
    return {
      content: String(s?.content ?? ""),
      status: (s?.status as "pending" | "in_progress" | "completed") ?? "pending",
    };
  });
  return (
    <div className="plan-body">
      {kv(t("agent.plan.goal"), plan?.goal)}
      {kv(t("agent.plan.candidateSites"), plan?.candidate_sites)}
      {kv(t("agent.plan.scope"), plan?.scope)}
      {kv(t("agent.plan.method"), plan?.method)}
      {kv(t("agent.plan.loginRequired"), plan?.login_required === undefined ? undefined : plan?.login_required ? t("common.yes") : t("common.no"))}
      {kv(t("agent.plan.dataFields"), plan?.data_fields)}
      {steps.length > 0 ? (
        <ul className="todo-list plan-steps">
          {steps.map((s, i) => (
            <li key={i} className={`todo-item ${s.status}`}>
              <TodoBadge status={s.status} />
              <span className="todo-content">{s.content}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {plan?.assumptions ? <div className="plan-assumptions">{t("agent.plan.assumptions", { text: String(plan.assumptions) })}</div> : null}
    </div>
  );
}

/** 运行中: 悬浮在输入框上方的规划面板; 顶部拖拽手柄可上下调整高度, 便于完整查看开发规划 */
function PlanPanel({ plan, onClose }: {
  plan: Record<string, unknown> | null;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [collapsed, setCollapsed] = useState(false);
  const [height, setHeight] = useState<number | null>(null);
  const rawSteps = Array.isArray(plan?.steps) ? plan.steps : [];
  const steps = rawSteps.map((s) => {
    if (typeof s === "string") return { content: s, status: "pending" };
    return { content: String(s?.content ?? ""), status: s?.status ?? "pending" };
  });
  const done = steps.filter((s) => s.status === "completed").length;
  const inProgress = steps.filter((s) => s.status === "in_progress").length;
  const pct = steps.length ? Math.round((done / steps.length) * 100) : 0;

  // 顶部手柄拖拽: 上拖放大 / 下拖缩小, 高度按父面板剩余空间钳制
  const beginResize = (e: React.MouseEvent<HTMLDivElement>) => {
    if (collapsed) return;
    e.preventDefault();
    const panel = (e.currentTarget as HTMLElement).closest(".agent-panel") as HTMLElement | null;
    const pane = (e.currentTarget as HTMLElement).closest(".plan-float") as HTMLElement | null;
    if (!panel || !pane) return;
    const startY = e.clientY;
    const startH = pane.offsetHeight;
    const titlebar = panel.querySelector(".agent-titlebar") as HTMLElement | null;
    const input = panel.querySelector(".agent-input-bar") as HTMLElement | null;
    const minH = 120;
    const maxH = Math.max(
      minH,
      panel.clientHeight
        - (titlebar?.offsetHeight ?? 0)
        - (input?.offsetHeight ?? 0)
        - 64,
    );
    const clamp = (h: number) => Math.max(minH, Math.min(maxH, h));
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    const onMove = (ev: MouseEvent) => setHeight(clamp(startH + (startY - ev.clientY)));
    const onUp = () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  return (
    <div
      className={`plan-float${collapsed ? " collapsed" : ""}`}
      style={!collapsed && height !== null ? { flex: `0 0 ${height}px`, maxHeight: height } : undefined}
    >
      <div className="plan-resize-handle" title={t("agent.plan.resize")} onMouseDown={beginResize} />
      <div className="plan-float-head">
        <span className="plan-float-title">{t("agent.plan.title")}</span>
        {steps.length > 0 ? (
          <span className="todo-progress plan-progress">
            <span className="todo-progress-bar" style={{ width: `${pct}%` }} />
          </span>
        ) : null}
        <span className="plan-float-pct">
          {steps.length
            ? inProgress > 0
              ? t("agent.plan.progressWithActive", { done, total: steps.length, active: inProgress, pct })
              : t("agent.plan.progress", { done, total: steps.length, pct })
            : ""}
        </span>
        <button
          className="plan-float-btn"
          title={collapsed ? t("agent.plan.expand") : t("agent.plan.collapse")}
          onClick={() => setCollapsed((c) => !c)}
        >
          {collapsed ? t("agent.plan.expandBtn") : t("agent.plan.collapseBtn")}
        </button>
        <button className="plan-float-btn" title={t("common.close")} onClick={onClose}>✕</button>
      </div>
      {!collapsed ? <PlanBody plan={plan} /> : null}
    </div>
  );
}

/** 任务完成后: 展示在所属 AGENT 回答上方的规划卡片 */
function PlanCard({ plan }: { plan: AgentPlan | null }) {
  const { t } = useI18n();
  if (!plan) return null;
  const rawSteps = Array.isArray(plan.steps) ? plan.steps : [];
  const steps = rawSteps.map((s) => {
    if (typeof s === "string") return { content: s, status: "pending" };
    return { content: String(s?.content ?? ""), status: s?.status ?? "pending" };
  });
  const done = steps.filter((s) => s.status === "completed").length;
  const pct = steps.length ? Math.round((done / steps.length) * 100) : 0;
  return (
    <div className="agent-card plan-card">
      <div className="agent-card-title">
        {t("agent.plan.title")}
        {steps.length > 0 ? (
          <span className="todo-progress plan-progress">
            <span className="todo-progress-bar" style={{ width: `${pct}%` }} />
          </span>
        ) : null}
      </div>
      <PlanBody plan={plan} />
    </div>
  );
}

function TodoCard({ todos }: { todos: { content: string; status: "pending" | "in_progress" | "completed" }[] }) {
  const { t } = useI18n();
  if (!todos.length) return null;
  const done = todos.filter((td) => td.status === "completed").length;
  const pct = Math.round((done / todos.length) * 100);
  return (
    <div className="agent-card todo-card">
      <div className="agent-card-title">
        {t("agent.todo.title", { done, total: todos.length })}
        <span className="todo-progress">
          <span className="todo-progress-bar" style={{ width: `${pct}%` }} />
        </span>
      </div>
      <ul className="todo-list">
        {todos.map((td, i) => (
          <li key={i} className={`todo-item ${td.status}`}>
            <TodoBadge status={td.status} />
            <span className="todo-content">{td.content}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 把单个答案格式化成直白的文本: 数组用顿号连接, 布尔转是/否, 空值显示占位。 */
function formatAnswer(value: unknown): string {
  if (value === null || value === undefined) return t("agent.answer.empty");
  if (Array.isArray(value)) {
    const items = value
      .map((v) => formatAnswer(v))
      .filter((s) => s && s !== t("agent.answer.empty"));
    return items.length ? items.join("、") : t("agent.answer.empty");
  }
  if (typeof value === "boolean") return value ? t("common.yes") : t("common.no");
  if (typeof value === "object") return JSON.stringify(value);
  const text = String(value).trim();
  return text === "" ? t("agent.answer.empty") : text;
}

/** 已完成的问卷: 以「问题 + 用户答案」列表展示, 不显示工具返回的原始 JSON。 */
function AnswerCard({ item }: { item: AgentFeedItem }) {
  const { t } = useI18n();
  const questions = item.answer?.questions ?? [];
  const rawAnswers = item.answer?.answers;
  const answers: Record<string, unknown> =
    rawAnswers && typeof rawAnswers === "object" && !Array.isArray(rawAnswers)
      ? (rawAnswers as Record<string, unknown>)
      : {};
  const rows: { title: string; value: unknown }[] = questions.map((q) => ({
    title: q.title || q.key,
    value: answers[q.key],
  }));
  const known = new Set(questions.map((q) => q.key));
  for (const [key, value] of Object.entries(answers)) {
    // _empty 是后端在用户未填写任何内容时的占位标记, 不作为答案展示
    if (key === "_empty" || known.has(key)) continue;
    rows.push({ title: key, value });
  }
  return (
    <div className="agent-card answer-card">
      <div className="agent-card-title">{t("agent.answer.title")}</div>
      {rows.length ? (
        <ol className="answer-list">
          {rows.map((row, i) => (
            <li key={i} className="answer-item">
              <div className="answer-question">{i + 1}. {row.title}</div>
              <div className="answer-value">
                <span className="answer-check">✓</span>
                <span>{formatAnswer(row.value)}</span>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <div className="answer-value muted">{t("agent.answer.empty")}</div>
      )}
    </div>
  );
}

function QuestionForm({ questions, qid, onSubmit }: {
  questions: AgentQuestion[];
  qid: string | null;
  onSubmit: (answers: Record<string, unknown>) => void;
}) {
  const { t } = useI18n();
  // 单选/填空的答案
  const [values, setValues] = useState<Record<string, unknown>>({});
  // 多选已勾选的候选选项(不含自定义答案)
  const [multi, setMulti] = useState<Record<string, string[]>>({});
  // 自定义选项的输入内容与选中状态
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [customOn, setCustomOn] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);

  const setVal = (key: string, v: unknown) => setValues((prev) => ({ ...prev, [key]: v }));
  const clearError = (key: string) =>
    setErrors((prev) => (prev[key] ? { ...prev, [key]: false } : prev));
  const setCustomOnFor = (key: string, on: boolean) => {
    setCustomOn((prev) => ({ ...prev, [key]: on }));
    clearError(key);
  };
  const setCustomText = (key: string, text: string) => {
    setCustom((prev) => ({ ...prev, [key]: text }));
    clearError(key);
  };
  // 选中自定义选项后离开输入框时校验: 答案不能为空(或只有空白字符)
  const checkCustom = (key: string) => {
    if (!(custom[key] ?? "").trim()) {
      setErrors((prev) => ({ ...prev, [key]: true }));
    }
  };
  // 选择类问题默认提供「自定义」选项, allow_custom 为 false 时关闭
  const allowCustom = (q: AgentQuestion) => q.allow_custom !== false;

  useEffect(() => {
    const v: Record<string, unknown> = {};
    const m: Record<string, string[]> = {};
    for (const q of questions) {
      if (q.type === "multi") {
        m[q.key] = Array.isArray(q.default)
          ? q.default.filter((x): x is string => typeof x === "string")
          : [];
      } else if (typeof q.default === "string" && q.default !== "") {
        v[q.key] = q.default;
      }
    }
    setValues(v);
    setMulti(m);
    setCustom({});
    setCustomOn({});
    setErrors({});
  }, [questions]);

  const handleSubmit = () => {
    const answers: Record<string, unknown> = {};
    const nextErrors: Record<string, boolean> = {};
    let invalid = false;
    for (const q of questions) {
      const useCustom = allowCustom(q) && Boolean(customOn[q.key]);
      if (q.type === "multi") {
        const picked = [...(multi[q.key] ?? [])];
        if (useCustom) {
          const text = (custom[q.key] ?? "").trim();
          if (!text) {
            nextErrors[q.key] = true;
            invalid = true;
          } else {
            picked.push(text);
          }
        }
        answers[q.key] = picked;
      } else if (q.type === "single") {
        if (useCustom) {
          const text = (custom[q.key] ?? "").trim();
          if (!text) {
            nextErrors[q.key] = true;
            invalid = true;
          }
          answers[q.key] = text;
        } else {
          answers[q.key] = values[q.key] ?? (typeof q.default === "string" ? q.default : "");
        }
      } else {
        answers[q.key] = (values[q.key] as string) ?? "";
      }
    }
    setErrors(nextErrors);
    // 选中「自定义」但没填写(或只有空白字符)时阻止提交
    if (invalid) return;
    setBusy(true);
    onSubmit(answers);
    setTimeout(() => setBusy(false), 500);
  };

  return (
    <div className="agent-card question-card">
      <div className="agent-card-title">{t("agent.question.title")}</div>
      {questions.map((q, i) => {
        const isCustom = allowCustom(q) && Boolean(customOn[q.key]);
        if (q.type === "text") {
          return (
            <div key={q.key} className="question-item">
              <div className="question-title">{i + 1}. {q.title}</div>
              <input
                type="text"
                value={(values[q.key] as string) ?? ""}
                onChange={(e) => setVal(q.key, e.target.value)}
                placeholder={t("agent.question.placeholder")}
              />
            </div>
          );
        }
        if (q.type === "multi") {
          return (
            <div key={q.key} className="question-item">
              <div className="question-title">{i + 1}. {q.title} {t("agent.question.multi")}</div>
              {(q.options ?? []).map((opt) => {
                const picked = multi[q.key] ?? [];
                const checked = picked.includes(opt);
                return (
                  <label key={opt} className="q-option">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() =>
                        setMulti((prev) => {
                          const cur = prev[q.key] ?? [];
                          return {
                            ...prev,
                            [q.key]: checked ? cur.filter((c) => c !== opt) : [...cur, opt],
                          };
                        })
                      }
                    />
                    <span>{opt}</span>
                  </label>
                );
              })}
              {allowCustom(q) ? (
                <div className={`q-custom${isCustom ? " on" : ""}`}>
                  <label className="q-option">
                    <input
                      type="checkbox"
                      checked={isCustom}
                      onChange={() => setCustomOnFor(q.key, !isCustom)}
                    />
                    <span>{t("agent.question.custom")}</span>
                  </label>
                  <input
                    type="text"
                    className={`q-custom-input${errors[q.key] ? " err" : ""}`}
                    disabled={!isCustom}
                    value={custom[q.key] ?? ""}
                    placeholder={t("agent.question.customPlaceholder")}
                    onChange={(e) => setCustomText(q.key, e.target.value)}
                    onBlur={() => {
                      if (isCustom) checkCustom(q.key);
                    }}
                  />
                </div>
              ) : null}
              {errors[q.key] ? <div className="q-error">{t("agent.question.customRequired")}</div> : null}
            </div>
          );
        }
        return (
          <div key={q.key} className="question-item">
            <div className="question-title">{i + 1}. {q.title}</div>
            {(q.options ?? []).map((opt) => (
              <label key={opt} className="q-option">
                <input
                  type="radio"
                  name={`q_${q.key}`}
                  checked={!isCustom && values[q.key] === opt}
                  onChange={() => {
                    setCustomOnFor(q.key, false);
                    setVal(q.key, opt);
                  }}
                />
                <span>{opt}</span>
              </label>
            ))}
            {allowCustom(q) ? (
              <div className={`q-custom${isCustom ? " on" : ""}`}>
                <label className="q-option">
                  <input
                    type="radio"
                    name={`q_${q.key}`}
                    checked={isCustom}
                    onChange={() => setCustomOnFor(q.key, true)}
                  />
                  <span>{t("agent.question.custom")}</span>
                </label>
                <input
                  type="text"
                  className={`q-custom-input${errors[q.key] ? " err" : ""}`}
                  disabled={!isCustom}
                  value={custom[q.key] ?? ""}
                  placeholder={t("agent.question.customPlaceholder")}
                  onChange={(e) => setCustomText(q.key, e.target.value)}
                  onBlur={() => {
                    if (isCustom) checkCustom(q.key);
                  }}
                />
              </div>
            ) : null}
            {errors[q.key] ? <div className="q-error">{t("agent.question.customRequired")}</div> : null}
          </div>
        );
      })}
      <button className="primary" disabled={!qid || busy} onClick={handleSubmit}>
        {t("agent.question.submit")}
      </button>
    </div>
  );
}

/* ---------------- 登录引导表单 (page_login) ---------------- */

const LOGIN_FLOAT_WIDTH = 380;
const LOGIN_FLOAT_TOP_RATIO = 0.12;

// zoom_browser 时画面会被放大盖满屏幕, 登录卡片以浮窗形式悬浮于放大画面之上, 可拖动/收起
function LoginForm({ login, onSubmit, onCancel, onSendCode, onRefreshCaptcha, onRefreshQr }: {
  login: AgentLoginRequest;
  onSubmit: (answers: Record<string, unknown>) => void;
  onCancel: () => void;
  onSendCode: () => void;
  onRefreshCaptcha: () => void;
  onRefreshQr: () => void;
}) {
  const { t } = useI18n();
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [countdown, setCountdown] = useState(0);
  const [busy, setBusy] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [pos, setPos] = useState(() => ({
    x: Math.max(8, Math.round((window.innerWidth - LOGIN_FLOAT_WIDTH) / 2)),
    y: Math.max(8, Math.round(window.innerHeight * LOGIN_FLOAT_TOP_RATIO)),
  }));
  const posRef = useRef(pos);
  posRef.current = pos;

  useEffect(() => {
    if (countdown <= 0) return;
    const t = setTimeout(() => setCountdown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [countdown]);

  const floating = Boolean(login.zoom_browser);

  const beginDrag = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!floating) return;
    if ((e.target as HTMLElement).closest(".login-float-collapse")) return;
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const start = { ...posRef.current };
    document.body.style.cursor = "move";
    document.body.style.userSelect = "none";
    const onMove = (ev: MouseEvent) => {
      const x = Math.min(window.innerWidth - 90, Math.max(8, start.x + (ev.clientX - startX)));
      const y = Math.min(window.innerHeight - 40, Math.max(8, start.y + (ev.clientY - startY)));
      setPos({ x, y });
    };
    const onUp = () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const setVal = (key: string, v: unknown) => setValues((prev) => ({ ...prev, [key]: v }));

  const wrap = (card: React.ReactNode) =>
    floating ? (
      <div className="login-float-card" style={{ left: pos.x, top: pos.y }}>
        <div className="login-float-title" onMouseDown={beginDrag}>
          <span>🔐 {login.login_type === "qr" ? t("login.qrTitle") : t("login.accountTitle")}</span>
          <button
            className="login-float-collapse"
            title={t("login.collapseHint")}
            onClick={() => setCollapsed(true)}
          >
            <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
              <path d="M3 6l5 5 5-5" />
            </svg>
          </button>
        </div>
        {card}
      </div>
    ) : (
      <>{card}</>
    );

  if (collapsed && floating) {
    return (
      <button
        className="login-float-pill"
        style={{ left: pos.x, top: pos.y }}
        title={t("login.expand")}
        onClick={() => setCollapsed(false)}
      >
        {t("login.loggingInCollapsed")}
      </button>
    );
  }

  if (login.login_type === "qr") {
    return wrap(
      <div className="agent-card question-card login-card">
        <div className="agent-card-title">🔐 {t("login.qrTitle")}</div>
        <div className="question-title">
          {login.message ?? t("login.qrHint")}
        </div>
        <div className="question-title login-hint">
          {t("login.agentQrSub")}
        </div>
        <div className="login-actions">
          <button
            className="primary"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              onSubmit({ ok: true });
              setTimeout(() => setBusy(false), 500);
            }}
          >
            {t("login.qrDone")}
          </button>
          <button onClick={onRefreshQr}>{t("login.refreshQr")}</button>
          <button onClick={onCancel}>{t("login.cancel")}</button>
        </div>
      </div>,
    );
  }

  return wrap(
    <div className="agent-card question-card login-card">
      <div className="agent-card-title">🔐 {t("login.accountTitle")}</div>
      {(login.fields ?? []).map((f) => (
        <div key={f.key} className="question-item">
          <div className="question-title">{f.label}</div>
          <input
            type={f.input_type === "password" ? "password" : "text"}
            value={(values[f.key] as string) ?? ""}
            placeholder={f.placeholder || t("login.enterField", { label: f.label })}
            onChange={(e) => setVal(f.key, e.target.value)}
          />
        </div>
      ))}
      {login.captcha && login.captcha.type !== "none" ? (
        <div className="question-item">
          <div className="question-title">{t("login.captcha")}</div>
          <div className="login-captcha-row">
            <input
              type="text"
              value={(values.captcha as string) ?? ""}
              placeholder={t("login.enterCaptcha")}
              onChange={(e) => setVal("captcha", e.target.value)}
            />
            {login.captcha.type === "image" && login.captcha.image ? (
              <img
                className="login-captcha-img"
                src={login.captcha.image}
                alt={t("login.captcha")}
                title={t("login.clickRefresh")}
                onClick={onRefreshCaptcha}
              />
            ) : null}
          </div>
          {login.captcha.type === "sms" ? (
            <button
              className="login-send-code"
              disabled={countdown > 0}
              onClick={() => {
                setCountdown(60);
                onSendCode();
              }}
            >
              {countdown > 0 ? t("login.resend", { count: countdown }) : t("login.sendCode")}
            </button>
          ) : null}
          {login.captcha.type === "image" ? (
            <button className="login-send-code" onClick={onRefreshCaptcha}>
              {t("login.changeImage")}
            </button>
          ) : null}
        </div>
      ) : null}
      <div className="login-actions">
        <button
          className="primary"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            onSubmit(values);
            setTimeout(() => setBusy(false), 500);
          }}
        >
          {login.submit_label ? t("login.submitLabel", { label: login.submit_label }) : t("login.submit")}
        </button>
        <button onClick={onCancel}>{t("login.cancel")}</button>
      </div>
    </div>,
  );
}

/* ---------------- 标题栏: Agent 状态图标 + 会话下拉框 ---------------- */

const RUNNING_STATUSES = new Set(["running", "waiting"]);

type SessionIconState = "running" | "busy" | "idle";

/** 会话/Agent 可用状态: 运行中 / 被其它会话占用 / 空闲可用 */
function sessionIconState(
  sessions: { id: string; status: string }[],
  sid: string | null,
): SessionIconState {
  const anyRunning = sessions.some((s) => RUNNING_STATUSES.has(s.status));
  if (sid) {
    const cur = sessions.find((s) => s.id === sid)?.status ?? "";
    if (RUNNING_STATUSES.has(cur)) return "running";
  }
  if (anyRunning) return "busy";
  return "idle";
}

function StatusIcon({ state, label }: { state: SessionIconState; label: string }) {
  return (
    <span className="agent-status-icon" title={label}>
      {state === "running" ? (
        <span className="agent-status-spin" />
      ) : (
        <span className={`agent-status-dot ${state}`} />
      )}
    </span>
  );
}

export interface SessionDropdownHandle {
  /** 进入当前激活会话的标题编辑模式 */
  editActive: () => void;
}

const SessionDropdown = forwardRef<SessionDropdownHandle, {
  activeId: string | null;
  sessions: { id: string; title: string; status: string; message_count: number }[];
  unread: Record<string, boolean>;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => Promise<void>;
}>(function SessionDropdown({
  activeId, sessions, unread, onSelect, onNew, onDelete, onRename,
}, ref) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [renaming, setRenaming] = useState(false);
  const selectRef = useRef<HTMLDivElement | null>(null);
  const [menuPos, setMenuPos] = useState<{ top: number; left: number } | null>(null);
  const activeTitle = activeId
    ? (sessions.find((s) => s.id === activeId)?.title ?? "")
    : "";
  const label = activeTitle || t("agent.newChat");
  const unreadCount = sessions.reduce(
    (n, s) => (s.id !== activeId && unread[s.id] ? n + 1 : n),
    0,
  );

  // 下拉菜单以 fixed 定位放到页面最上层(避免被 sidebar overflow:hidden 裁剪, 遮住编辑/删除按钮)。
  // 打开时根据按钮位置计算一次, 位置固定不随滚动漂移; 仅在窗口 resize 时校正, 避免布局抖动。
  const updateMenuPos = useCallback(() => {
    const el = selectRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const menuWidth = 240;
    let left = rect.left;
    if (left < 8) left = 8;
    if (left + menuWidth > window.innerWidth - 8) left = Math.max(8, window.innerWidth - menuWidth - 8);
    setMenuPos({ top: rect.bottom + 4, left });
  }, []);

  const toggleMenu = () => {
    if (open) {
      setOpen(false);
      return;
    }
    updateMenuPos();
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onResize = () => updateMenuPos();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
    };
  }, [open, updateMenuPos]);

  const dotState = (sid: string | null): SessionIconState => sessionIconState(sessions, sid);
  const dotLabel = (state: SessionIconState) =>
    state === "running" ? t("agent.dot.running") : state === "busy" ? t("agent.dot.busy") : t("agent.dot.idle");

  const pick = (fn: () => void) => {
    setOpen(false);
    fn();
  };

  const startEdit = (id: string, title: string, keepOpen = true) => {
    setEditingId(id);
    setEditValue(title);
    if (keepOpen) setOpen(true);
  };

  const confirmEdit = async () => {
    const id = editingId;
    const title = editValue.trim();
    setRenaming(true);
    try {
      if (id && title) {
        await onRename(id, title);
        setEditingId(null);
      }
    } finally {
      setRenaming(false);
    }
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditValue("");
  };

  useImperativeHandle(ref, () => ({
    editActive: () => {
      if (!activeId) return;
      // 标题栏内联编辑, 不弹出下拉菜单
      setOpen(false);
      startEdit(activeId, activeTitle, false);
    },
  }));

  const editingActive = editingId !== null && editingId === activeId;

  return (
    <div className="agent-session-select" ref={selectRef}>
      {editingActive ? (
        <div className="agent-session-edit">
          <input
            className="agent-session-edit-input"
            value={editValue}
            autoFocus
            onChange={(e) => setEditValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void confirmEdit();
              if (e.key === "Escape") cancelEdit();
            }}
            onBlur={() => {
              if (!renaming) cancelEdit();
            }}
            placeholder={t("agent.titlePlaceholder")}
          />
          <button
            className="agent-session-edit-btn ok"
            title={t("agent.saveTitle")}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => void confirmEdit()}
          >
            ✓
          </button>
          <button
            className="agent-session-edit-btn"
            title={t("common.cancel")}
            onMouseDown={(e) => e.preventDefault()}
            onClick={cancelEdit}
          >
            ✕
          </button>
        </div>
      ) : (
        <button
          className="agent-session-select-btn"
          title={label}
          onClick={toggleMenu}
        >
          <span className="agent-session-select-label">{displayTitle(label)}</span>
          {unreadCount > 0 ? (
            <span className="agent-session-unread-badge" title={t("agent.unreadSessions", { count: unreadCount })}>
              {unreadCount}
            </span>
          ) : null}
          <span className="agent-session-select-caret">{open ? "▴" : "▾"}</span>
        </button>
      )}
      {open ? (
        <>
          <div className="agent-menu-backdrop" onClick={() => setOpen(false)} />
          <div
            className="agent-menu"
            role="menu"
            style={menuPos ? { top: menuPos.top, left: menuPos.left } : undefined}
          >
            <button
              className={`agent-menu-item${!activeId ? " active" : ""}`}
              role="menuitem"
              title={t("agent.newSessionTitle")}
              onClick={() => pick(onNew)}
            >
              <span className="agent-menu-dot">
                <span className={`agent-status-dot ${dotState(null)}`} />
              </span>
              <span className="agent-menu-title">{t("agent.newChat")}</span>
              <span className="agent-menu-count" title={dotLabel(dotState(null))}>
                {dotState(null) === "busy" ? t("agent.busy") : dotState(null) === "running" ? t("agent.running") : t("agent.available")}
              </span>
            </button>
            {sessions.length > 0 ? <div className="agent-menu-sep" /> : null}
            {sessions.map((s) => {
              const state = dotState(s.id);
              const editing = editingId === s.id;
              if (editing) {
                return (
                  <div
                    key={s.id}
                    className="agent-menu-item agent-menu-edit-item"
                    role="menuitem"
                    onMouseDown={(e) => e.stopPropagation()}
                  >
                    <span className="agent-menu-dot" />
                    <input
                      className="agent-session-edit-input"
                      value={editValue}
                      autoFocus
                      onChange={(e) => setEditValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void confirmEdit();
                        if (e.key === "Escape") cancelEdit();
                      }}
                      onBlur={() => {
                        if (!renaming) cancelEdit();
                      }}
                      placeholder={t("agent.titlePlaceholder")}
                    />
                    <button
                      className="agent-session-edit-btn ok"
                      title={t("agent.saveTitle")}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => void confirmEdit()}
                    >
                      ✓
                    </button>
                    <button
                      className="agent-session-edit-btn"
                      title={t("common.cancel")}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={cancelEdit}
                    >
                      ✕
                    </button>
                  </div>
                );
              }
              return (
                <div
                  key={s.id}
                  className={`agent-menu-item${activeId === s.id ? " active" : ""}`}
                  role="menuitem"
                  title={`${s.title} (${dotLabel(state)})`}
                  onClick={() => pick(() => onSelect(s.id))}
                >
                  <span className="agent-menu-dot">
                    {state === "running" ? (
                      <span className="agent-status-spin" />
                    ) : (
                      <span className={`agent-status-dot ${state}`} />
                    )}
                  </span>
                  <span className="agent-menu-title" title={s.title}>{displayTitle(s.title)}</span>
                  {unread[s.id] ? <span className="agent-menu-unread" title={t("agent.unread")} /> : null}
                  <span className="agent-menu-count">{s.message_count}</span>
                  <button
                    className="agent-session-edit"
                    title={t("agent.renameTitle")}
                    onClick={(e) => {
                      e.stopPropagation();
                      startEdit(s.id, s.title);
                    }}
                  >
                    <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="M11.2 2.8a1.7 1.7 0 0 1 2.4 2.4l-7.6 7.6-3 1 1-3 7.2-7Z" />
                    </svg>
                  </button>
                  <button
                    className="agent-session-del"
                    title={t("agent.deleteSession")}
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(s.id);
                    }}
                  >
                    ✕
                  </button>
                </div>
              );
            })}
          </div>
        </>
      ) : null}
    </div>
  );
});

export default function AgentPanel({
  onAgentCode,
  onClose,
}: {
  onAgentCode?: (code: string, base?: string | null) => void;
  onClose?: () => void;
}) {
  const agent = useAgent();
  const { t } = useI18n();
  const feedRef = useRef<HTMLDivElement | null>(null);
  const sessionDropdownRef = useRef<SessionDropdownHandle | null>(null);
  const [taskInput, setTaskInput] = useState("");
  const [sending, setSending] = useState(false);
  const [planDismissed, setPlanDismissed] = useState<Record<string, boolean>>({});
  const onAgentCodeRef = useRef(onAgentCode);
  onAgentCodeRef.current = onAgentCode;

  useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [agent.feed, agent.streaming, agent.todos, agent.questions]);

  // Agent 每次 set_editor_code: 把写回的代码同步到编辑器
  useEffect(() => {
    if (agent.editorCode !== null) {
      onAgentCodeRef.current?.(agent.editorCode);
    }
  }, [agent.editorCode]);

  // 新一轮对话开始 → 重新展示规划面板(新的一轮可能生成新规划)
  useEffect(() => {
    setPlanDismissed({});
  }, [agent.turnSeq]);

  const busy = agent.status === "running" || agent.status === "waiting";
  const hasSession = Boolean(agent.activeId);
  const showPlan = Boolean(agent.activeId && agent.plan && !planDismissed[agent.activeId]);

  // 标题栏状态图标: 当前会话运行中 → loading; 被其它会话占用 → 红灯; 否则绿灯
  const iconState = sessionIconState(agent.sessions, agent.activeId);
  const iconLabel =
    iconState === "running"
      ? t(STATUS_TEXT[agent.status])
      : iconState === "busy"
        ? t("agent.busyHint")
        : t("agent.idleHint");

  const dismissPlan = () => {
    const sid = agent.activeId;
    if (!sid) return;
    setPlanDismissed((prev) => ({ ...prev, [sid]: true }));
  };

  const handleSend = async () => {
    const text = taskInput.trim();
    if (!text || busy || sending) return;
    setSending(true);
    try {
      if (!hasSession) {
        await agent.createSession();
      }
      setTaskInput("");
      await agent.sendMessage(text);
    } finally {
      setSending(false);
    }
  };

  const handleNew = () => {
    agent.startNewSession();
  };

  return (
    <div className={`agent-panel${busy ? " running" : ""}`}>
      <div className="agent-titlebar">
        <span className="agent-title">{t("activity.agent")}</span>
        <StatusIcon state={iconState} label={iconLabel} />
        {agent.crawlerId ? <span className="agent-crawler-tag">{agent.crawlerId}</span> : null}
        <SessionDropdown
          ref={sessionDropdownRef}
          activeId={agent.activeId}
          sessions={agent.sessions}
          unread={agent.unread}
          onSelect={(id) => agent.selectSession(id)}
          onNew={handleNew}
          onDelete={(id) => void agent.deleteSession(id)}
          onRename={(id, title) => agent.renameSession(id, title)}
        />
        {hasSession ? (
          <>
            <button
              className="agent-session-new"
              title={t("agent.editTitle")}
              onClick={() => sessionDropdownRef.current?.editActive()}
            >
              <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M11.2 2.8a1.7 1.7 0 0 1 2.4 2.4l-7.6 7.6-3 1 1-3 7.2-7Z" />
              </svg>
            </button>
            <button
              className="agent-session-new"
              title={t("agent.newSessionTitle")}
              onClick={handleNew}
            >
              <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M8 2.5a5.5 5.5 0 0 0 0 11c.8 0 1.5-.1 2.2-.4l2.8.9-.9-2.8c.7-.9 1.1-2.1 1.1-3.2A5.5 5.5 0 0 0 8 2.5Z" />
                <path d="M8 6v4M6 8h4" />
              </svg>
            </button>
          </>
        ) : null}
        {!agent.connected ? <span className="agent-offline" title={t("agent.offlineTitle")}>{t("agent.offline")}</span> : null}
        <span className="agent-titlebar-right">
          {onClose ? (
            <button className="close" title={t("sidebar.close")} onClick={onClose}>
              <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true">
                <path d="M12 4l-4 4 4 4-1 1-4-4-4 4-1-1 4-4-4-4 1-1 4 4 4-4 1 1z" />
              </svg>
            </button>
          ) : null}
        </span>
      </div>

      <div className="agent-feed" ref={feedRef}>
        {!hasSession ? (
          <div className="agent-welcome">
            <p className="hint">{t("agent.welcome")}</p>
          </div>
        ) : (
          <>
            <TodoCard todos={agent.todos} />
            {agent.feed.map((item, i) => <FeedItem key={i} item={item} />)}
            {agent.streaming ? <div className="oc-message streaming">{agent.streaming}<span className="caret" /></div> : null}
            {agent.questions.length > 0 ? (
              <QuestionForm
                questions={agent.questions}
                qid={agent.qid}
                onSubmit={(answers) => void agent.submitAnswer(answers)}
              />
            ) : null}
            {agent.login ? (
              <LoginForm
                key={agent.login.qid}
                login={agent.login}
                onSubmit={(answers) => void agent.submitLogin(answers)}
                onCancel={() => void agent.cancelLogin()}
                onSendCode={() => void agent.sendLoginCode()}
                onRefreshCaptcha={() => void agent.refreshCaptcha()}
                onRefreshQr={() => void agent.refreshQr()}
              />
            ) : null}
          </>
        )}
      </div>

      {showPlan ? <PlanPanel plan={agent.plan} onClose={dismissPlan} /> : null}
      <div className="agent-input-bar">
        {hasSession && !busy && agent.activeSession ? (
          <span className="hint agent-done-hint">{t("agent.continueHint")}</span>
        ) : null}
        <textarea
          value={taskInput}
          onChange={(e) => setTaskInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void handleSend();
          }}
          placeholder={t("agent.inputPlaceholder")}
          rows={2}
        />
        {busy ? (
          <button className="danger send-btn" onClick={() => void agent.stop()}>
            {t("agent.stop")}
          </button>
        ) : (
          <button
            className="primary send-btn"
            onClick={() => void handleSend()}
            disabled={!taskInput.trim() || sending}
          >
            {sending ? t("agent.sending") : hasSession ? t("agent.send") : t("agent.createAndSend")}
          </button>
        )}
      </div>
    </div>
  );
}
