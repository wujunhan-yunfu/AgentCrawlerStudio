import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import type { AgentFeedItem, AgentLoginRequest, AgentPlan, AgentQuestion, AgentStatus } from "../types";
import { useAgent } from "../hooks/useAgent";
import AgentDiffCard from "./AgentDiffCard";

const STATUS_TEXT: Record<AgentStatus, string> = {
  idle: "待命",
  running: "执行中",
  waiting: "等待确认",
  done: "已完成",
  error: "出错",
  cancelled: "已取消",
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
  get_editor_code: { icon: "→", summary: () => "Read 编辑器代码" },
  set_editor_code: { icon: "←", summary: () => "Edit 编辑器" },
  http_request: {
    icon: "%",
    summary: (a) => `${str(a?.method || "GET")} ${truncate(str(a?.url), 50)}`,
  },
  record_plan: { icon: "✓", summary: () => "记录爬取规划" },
  ask_user: { icon: "→", summary: () => "需要你确认" },
  page_login: { icon: "🔐", summary: () => "需要用户登录" },
  archive_content: {
    icon: "⇩",
    summary: (a) => `Archive${a?.fmt ? ` (${str(a.fmt)})` : ""} 内容`,
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
  if (item.kind === "error") {
    return <div className="oc-message err">{item.content}</div>;
  }
  return null;
}

function PlanBody({ plan }: { plan: AgentPlan | null }) {
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
      {kv("目标", plan?.goal)}
      {kv("候选网站", plan?.candidate_sites)}
      {kv("爬取范围", plan?.scope)}
      {kv("爬取方式", plan?.method)}
      {kv("需要登录", plan?.login_required === undefined ? undefined : plan?.login_required ? "是" : "否")}
      {kv("数据字段", plan?.data_fields)}
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
      {plan?.assumptions ? <div className="plan-assumptions">假设: {String(plan.assumptions)}</div> : null}
    </div>
  );
}

/** 运行中: 输入容器内的规划面板(向上展开时缩小输入框高度) */
function PlanPanel({ plan, onClose }: {
  plan: Record<string, unknown> | null;
  onClose: () => void;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const rawSteps = Array.isArray(plan?.steps) ? plan.steps : [];
  const steps = rawSteps.map((s) => {
    if (typeof s === "string") return { content: s, status: "pending" };
    return { content: String(s?.content ?? ""), status: s?.status ?? "pending" };
  });
  const done = steps.filter((s) => s.status === "completed").length;
  const inProgress = steps.filter((s) => s.status === "in_progress").length;
  const pct = steps.length ? Math.round((done / steps.length) * 100) : 0;

  return (
    <div className={`plan-float${collapsed ? " collapsed" : ""}`}>
      <div className="plan-float-head">
        <span className="plan-float-title">📋 爬取规划</span>
        {steps.length > 0 ? (
          <span className="todo-progress plan-progress">
            <span className="todo-progress-bar" style={{ width: `${pct}%` }} />
          </span>
        ) : null}
        <span className="plan-float-pct">
          {steps.length ? `已完成 ${done}/${steps.length}${inProgress ? `, 进行中 ${inProgress}` : ""} · ${pct}%` : ""}
        </span>
        <button
          className="plan-float-btn"
          title={collapsed ? "展开" : "折叠"}
          onClick={() => setCollapsed((c) => !c)}
        >
          {collapsed ? "▾ 展开" : "▴ 折叠"}
        </button>
        <button className="plan-float-btn" title="关闭" onClick={onClose}>✕</button>
      </div>
      {!collapsed ? <PlanBody plan={plan} /> : null}
    </div>
  );
}

/** 任务完成后: 展示在所属 AGENT 回答上方的规划卡片 */
function PlanCard({ plan }: { plan: AgentPlan | null }) {
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
        📋 爬取规划
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
  if (!todos.length) return null;
  const done = todos.filter((t) => t.status === "completed").length;
  const pct = Math.round((done / todos.length) * 100);
  return (
    <div className="agent-card todo-card">
      <div className="agent-card-title">
        ✅ 任务清单 ({done}/{todos.length})
        <span className="todo-progress">
          <span className="todo-progress-bar" style={{ width: `${pct}%` }} />
        </span>
      </div>
      <ul className="todo-list">
        {todos.map((t, i) => (
          <li key={i} className={`todo-item ${t.status}`}>
            <TodoBadge status={t.status} />
            <span className="todo-content">{t.content}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function QuestionForm({ questions, qid, onSubmit }: {
  questions: AgentQuestion[];
  qid: string | null;
  onSubmit: (answers: Record<string, unknown>) => void;
}) {
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [multi, setMulti] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);

  const setVal = (key: string, v: unknown) => setValues((prev) => ({ ...prev, [key]: v }));

  useEffect(() => {
    const defaults: Record<string, unknown> = {};
    for (const q of questions) {
      if (q.type === "multi") {
        const pre = Array.isArray(q.default) ? (q.default as string[]) : [];
        if (pre.length) defaults[q.key] = pre;
      } else if (q.default !== undefined && q.default !== null && q.default !== "") {
        defaults[q.key] = q.default;
      }
    }
    if (Object.keys(defaults).length) setValues((prev) => ({ ...defaults, ...prev }));
  }, [questions]);

  const handleSubmit = () => {
    setBusy(true);
    const answers: Record<string, unknown> = { ...values };
    for (const q of questions) {
      if (q.type === "multi") answers[q.key] = multi[q.key] ?? [];
    }
    onSubmit(answers);
    setTimeout(() => setBusy(false), 500);
  };

  return (
    <div className="agent-card question-card">
      <div className="agent-card-title">❓ 需要你的确认</div>
      {questions.map((q, i) => {
        const def = q.default ?? "";
        if (q.type === "text") {
          return (
            <div key={q.key} className="question-item">
              <div className="question-title">{i + 1}. {q.title}</div>
              <input
                type="text"
                value={(values[q.key] as string) ?? ""}
                onChange={(e) => setVal(q.key, e.target.value)}
                placeholder="请填写..."
              />
            </div>
          );
        }
        if (q.type === "multi") {
          return (
            <div key={q.key} className="question-item">
              <div className="question-title">{i + 1}. {q.title} (可多选)</div>
              {(q.options ?? []).map((opt) => {
                const checked = (multi[q.key] ?? []).includes(opt);
                return (
                  <label key={opt} className="q-option">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => {
                        const cur = multi[q.key] ?? [];
                        setMulti((prev) => ({
                          ...prev,
                          [q.key]: checked ? cur.filter((c) => c !== opt) : [...cur, opt],
                        }));
                      }}
                    />
                    <span>{opt}</span>
                  </label>
                );
              })}
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
                  defaultChecked={opt === def}
                  onChange={() => setVal(q.key, opt)}
                />
                <span>{opt}</span>
              </label>
            ))}
          </div>
        );
      })}
      <button className="primary" disabled={!qid || busy} onClick={handleSubmit}>
        提交确认
      </button>
    </div>
  );
}

/* ---------------- 登录引导表单 (page_login) ---------------- */

function LoginForm({ login, onSubmit, onCancel, onSendCode, onRefreshCaptcha, onRefreshQr }: {
  login: AgentLoginRequest;
  onSubmit: (answers: Record<string, unknown>) => void;
  onCancel: () => void;
  onSendCode: () => void;
  onRefreshCaptcha: () => void;
  onRefreshQr: () => void;
}) {
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [countdown, setCountdown] = useState(0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (countdown <= 0) return;
    const t = setTimeout(() => setCountdown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [countdown]);

  const setVal = (key: string, v: unknown) => setValues((prev) => ({ ...prev, [key]: v }));

  if (login.login_type === "qr") {
    return (
      <div className="agent-card question-card login-card">
        <div className="agent-card-title">🔐 二维码登录</div>
        <div className="question-title">
          {login.message ?? "请在放大后的浏览器实时画面中用手机 APP 扫码登录。"}
        </div>
        <div className="question-title login-hint">
          系统已放大浏览器实时画面并持续监听登录跳转，扫码成功后 Agent 会自动继续。
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
            我已经完成扫码，继续
          </button>
          <button onClick={onRefreshQr}>刷新二维码</button>
          <button onClick={onCancel}>取消登录</button>
        </div>
      </div>
    );
  }

  return (
    <div className="agent-card question-card login-card">
      <div className="agent-card-title">🔐 账号登录</div>
      {(login.fields ?? []).map((f) => (
        <div key={f.key} className="question-item">
          <div className="question-title">{f.label}</div>
          <input
            type={f.input_type === "password" ? "password" : "text"}
            value={(values[f.key] as string) ?? ""}
            placeholder={f.placeholder || `请输入${f.label}`}
            onChange={(e) => setVal(f.key, e.target.value)}
          />
        </div>
      ))}
      {login.captcha && login.captcha.type !== "none" ? (
        <div className="question-item">
          <div className="question-title">验证码</div>
          <div className="login-captcha-row">
            <input
              type="text"
              value={(values.captcha as string) ?? ""}
              placeholder="请输入验证码"
              onChange={(e) => setVal("captcha", e.target.value)}
            />
            {login.captcha.type === "image" && login.captcha.image ? (
              <img
                className="login-captcha-img"
                src={login.captcha.image}
                alt="验证码"
                title="点击刷新"
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
              {countdown > 0 ? `${countdown}s 后重发` : "发送验证码"}
            </button>
          ) : null}
          {login.captcha.type === "image" ? (
            <button className="login-send-code" onClick={onRefreshCaptcha}>
              换一张
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
          {login.submit_label ? `提交${login.submit_label}` : "提交登录"}
        </button>
        <button onClick={onCancel}>取消登录</button>
      </div>
    </div>
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
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [renaming, setRenaming] = useState(false);
  const selectRef = useRef<HTMLDivElement | null>(null);
  const [menuPos, setMenuPos] = useState<{ top: number; left: number } | null>(null);
  const activeTitle = activeId
    ? (sessions.find((s) => s.id === activeId)?.title ?? "")
    : "";
  const label = activeTitle || "新对话";
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
    state === "running" ? "执行中" : state === "busy" ? "被占用" : "可用";

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
            placeholder="输入标题..."
          />
          <button
            className="agent-session-edit-btn ok"
            title="保存标题"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => void confirmEdit()}
          >
            ✓
          </button>
          <button
            className="agent-session-edit-btn"
            title="取消"
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
            <span className="agent-session-unread-badge" title={`${unreadCount} 个会话有未读消息`}>
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
              title="新建会话"
              onClick={() => pick(onNew)}
            >
              <span className="agent-menu-dot">
                <span className={`agent-status-dot ${dotState(null)}`} />
              </span>
              <span className="agent-menu-title">新对话</span>
              <span className="agent-menu-count" title={dotLabel(dotState(null))}>
                {dotState(null) === "busy" ? "忙" : dotState(null) === "running" ? "执行中" : "可用"}
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
                      placeholder="输入标题..."
                    />
                    <button
                      className="agent-session-edit-btn ok"
                      title="保存标题"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => void confirmEdit()}
                    >
                      ✓
                    </button>
                    <button
                      className="agent-session-edit-btn"
                      title="取消"
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
                  {unread[s.id] ? <span className="agent-menu-unread" title="有未读消息" /> : null}
                  <span className="agent-menu-count">{s.message_count}</span>
                  <button
                    className="agent-session-edit"
                    title="修改标题"
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
                    title="删除会话"
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
      ? STATUS_TEXT[agent.status]
      : iconState === "busy"
        ? "Agent 忙碌, 其他会话正在执行"
        : "Agent 空闲可用";

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
        <span className="agent-title">爬虫 Agent</span>
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
              title="修改当前会话标题"
              onClick={() => sessionDropdownRef.current?.editActive()}
            >
              <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M11.2 2.8a1.7 1.7 0 0 1 2.4 2.4l-7.6 7.6-3 1 1-3 7.2-7Z" />
              </svg>
            </button>
            <button
              className="agent-session-new"
              title="新建会话"
              onClick={handleNew}
            >
              <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M8 2.5a5.5 5.5 0 0 0 0 11c.8 0 1.5-.1 2.2-.4l2.8.9-.9-2.8c.7-.9 1.1-2.1 1.1-3.2A5.5 5.5 0 0 0 8 2.5Z" />
                <path d="M8 6v4M6 8h4" />
              </svg>
            </button>
          </>
        ) : null}
        {!agent.connected ? <span className="agent-offline" title="连接断开, 重连中...">断开</span> : null}
        <span className="agent-titlebar-right">
          {onClose ? (
            <button className="close" title="关闭面板" onClick={onClose}>
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
            <p className="hint">只需一句话告诉我你的爬虫需求, 我来帮你完成。</p>
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

      <div className="agent-input-bar">
        {showPlan ? <PlanPanel plan={agent.plan} onClose={dismissPlan} /> : null}
        {hasSession && !busy && agent.activeSession ? (
          <span className="hint agent-done-hint">当前会话可继续发送消息进行多轮对话</span>
        ) : null}
        <textarea
          value={taskInput}
          onChange={(e) => setTaskInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void handleSend();
          }}
          placeholder="描述你的需求, 如: 抓取某网站数据 / 优化编辑器里的爬虫代码 (Ctrl+Enter 发送)..."
          rows={2}
        />
        {busy ? (
          <button className="danger send-btn" onClick={() => void agent.stop()}>
            停止
          </button>
        ) : (
          <button
            className="primary send-btn"
            onClick={() => void handleSend()}
            disabled={!taskInput.trim() || sending}
          >
            {sending ? "发送中..." : hasSession ? "发送" : "新建并发送"}
          </button>
        )}
      </div>
    </div>
  );
}
