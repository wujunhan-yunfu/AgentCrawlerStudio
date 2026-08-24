import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AgentFeedItem,
  AgentLoginCaptcha,
  AgentLoginField,
  AgentLoginRequest,
  AgentPlan,
  AgentQuestion,
  AgentSavedItem,
  AgentSessionInfo,
  AgentStatus,
  AgentStoredMessage,
  AgentTodo,
} from "../types";
import {
  agentAnswer,
  agentCreateSession,
  agentDeleteSession,
  agentFinalize,
  agentGetMessages,
  agentLoginAction,
  agentLoginAnswer,
  agentRenameSession,
  agentSendMessage,
  agentStop,
  agentWsUrl,
} from "../utils/api";

interface AgentEvent {
  type: string;
  session_id?: string;
  crawler_id?: string;
  title?: string;
  qid?: string;
  questions?: AgentQuestion[];
  plan?: AgentPlan;
  todos?: AgentTodo[];
  content?: string;
  name?: string;
  args?: string;
  error?: string;
  result?: string;
  saved?: AgentSavedItem[];
  reason?: string;
  code?: string;
  base?: string;
  sessions?: AgentSessionInfo[];
  id?: string;
  login_type?: string;
  method?: string;
  message?: string;
  url?: string;
  action?: string;
  zoom_browser?: boolean;
  fields?: AgentLoginField[];
  captcha?: AgentLoginCaptcha;
  submit_label?: string;
  timeout?: number;
  ok?: boolean;
}

export interface AgentState {
  connected: boolean;
  crawlerId: string | null;
  sessions: AgentSessionInfo[];
  activeId: string | null;
  activeSession: AgentSessionInfo | null;
  status: AgentStatus;
  /** 有未读消息的会话: 会话下拉框显示角标 */
  unread: Record<string, boolean>;
  feed: AgentFeedItem[];
  streaming: string;
  plan: AgentPlan | null;
  todos: AgentTodo[];
  questions: AgentQuestion[];
  saved: AgentSavedItem[];
  login: AgentLoginRequest | null;
  qid: string | null;
  editorCode: string | null;
  editorBase: string | null;
  turnSeq: number;
  createSession: () => Promise<void>;
  startNewSession: () => void;
  selectSession: (id: string) => void;
  sendMessage: (content: string) => Promise<void>;
  submitAnswer: (answers: Record<string, unknown>) => Promise<void>;
  submitLogin: (answers: Record<string, unknown>) => Promise<void>;
  sendLoginCode: () => Promise<void>;
  refreshCaptcha: () => Promise<void>;
  cancelLogin: () => Promise<void>;
  stop: () => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
}

const EMPTY_SESSION_INFO = {
  crawler_id: "",
  title: "",
  status: "idle",
  message_count: 0,
  last_message: "",
};

// 不在 feed 中展示的工具: 其产出已通过 plan / todos 等卡片呈现
const HIDDEN_TOOL_NAMES = new Set(["write_todos"]);

// 未读消息标记的持久化: 关闭/刷新页面后仍能恢复「离开期间产生的未读角标」。
// 按 crawler_id 隔离, 键前缀 + crawler_id 作为 localStorage key。
const UNREAD_KEY_PREFIX = "agent.unread.";
const SEEN_KEY_PREFIX = "agent.seen.";

function loadJSON<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : fallback;
  } catch {
    return fallback;
  }
}

function saveJSON(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* 忽略: 隐私模式等 localStorage 不可用时静默降级 */
  }
}

// 会产生新内容、触发未读标记的事件类型
const ACTIVITY_TYPES = new Set([
  "delta",
  "message_end",
  "tool",
  "tool_result",
  "status",
  "plan",
  "todos",
  "saved",
  "question",
  "editor_code",
  "login_request",
  "login_success",
  "done",
  "error",
  "session_end",
]);

function buildFeedFromMessages(messages: AgentStoredMessage[]): {
  feed: AgentFeedItem[];
  plan: AgentPlan | null;
  todos: AgentTodo[];
  saved: AgentSavedItem[];
} {
  const feed: AgentFeedItem[] = [];
  let plan: AgentPlan | null = null;
  let todos: AgentTodo[] = [];
  let saved: AgentSavedItem[] = [];
  // 重建 diff 卡片时, 记录上一次 set_editor_code 写回的代码(跨轮次重置)
  let lastCode: string | null = null;
  // 记录当前轮次产生的规划, 遇到该轮 AGENT 回答时在回答上方插入规划卡片
  let pendingPlan: AgentPlan | null = null;

  const findTool = (id?: string, name?: string) => {
    if (id) {
      for (let i = feed.length - 1; i >= 0; i--) {
        const it = feed[i];
        if (it.kind === "tool" && it.id === id) return { index: i, item: it };
      }
    }
    if (name) {
      for (let i = feed.length - 1; i >= 0; i--) {
        const it = feed[i];
        if (it.kind === "tool" && it.name === name && (!it.state || it.state === "running")) {
          return { index: i, item: it };
        }
      }
    }
    return null;
  };

  for (const m of messages) {
    const meta = m.meta ?? {};
    if (m.role === "user") {
      if (pendingPlan) {
        feed.push({ kind: "plan", plan: pendingPlan });
        pendingPlan = null;
      }
      feed.push({ kind: "user", content: m.content });
      lastCode = null; // 新一轮对话: 重置 diff 起点
    } else if (m.role === "assistant") {
      // 该轮已完成: 规划显示在所属 AGENT 回答上方
      if (pendingPlan) {
        feed.push({ kind: "plan", plan: pendingPlan });
        pendingPlan = null;
      }
      feed.push({ kind: "message", content: m.content });
    } else if (m.type === "status") {
      feed.push({ kind: "status", content: m.content });
    } else if (m.type === "error") {
      if (pendingPlan) {
        feed.push({ kind: "plan", plan: pendingPlan });
        pendingPlan = null;
      }
      feed.push({ kind: "error", content: m.content });
    } else if (m.type === "plan") {
      pendingPlan = (meta.plan as AgentPlan) ?? null;
    } else if (m.type === "todos") {
      todos = (meta.todos as AgentTodo[]) ?? [];
    } else if (m.type === "saved") {
      const items = (meta.saved as AgentSavedItem[]) ?? [];
      saved = [...saved, ...items];
      feed.push({
        kind: "saved",
        saved: items,
        content: m.content || `已保存 ${items.length} 项内容`,
      });
    } else if (m.type === "tool") {
      const name = (meta.name as string) ?? "";
      if (HIDDEN_TOOL_NAMES.has(name)) continue;
      feed.push({
        kind: "tool",
        id: meta.id as string | undefined,
        name,
        args: m.content,
        state: "running",
      });
    } else if (m.type === "tool_result") {
      const id = meta.id as string | undefined;
      const name = (meta.name as string) ?? "";
      if (HIDDEN_TOOL_NAMES.has(name)) continue;
      const err = (meta.error as string) ?? "";
      const found = findTool(id, name);
      const patch = { state: err ? "error" : "done", content: m.content, error: err } as const;
      if (found) {
        feed[found.index] = { ...found.item, ...patch };
      } else {
        feed.push({
          kind: "tool",
          id,
          name,
          content: m.content,
          error: err,
          state: err ? "error" : "done",
        });
      }
    } else if (m.type === "editor_code") {
      const to = m.content;
      const base = (meta.base as string) ?? "";
      const from = lastCode !== null ? lastCode : base;
      lastCode = to;
      if (from !== to) {
        feed.push({ kind: "diff", diff: { from, to } });
      }
    } else if (m.type === "login_request") {
      feed.push({ kind: "status", content: "需要用户完成登录（请扫码或填写登录信息）" });
    } else if (m.type === "login_success") {
      feed.push({ kind: "status", content: m.content || "登录成功" });
    } else if (m.type === "login_action") {
      if (m.content) feed.push({ kind: "status", content: m.content });
    }
  }
  // 最后一轮尚未完成(历史恢复时仍在运行): 规划留在输入区展示, 不放入 feed
  if (pendingPlan) {
    plan = pendingPlan;
  }
  // 历史恢复时, 用最后一条 todos 的进度回填 plan.steps 的状态
  if (plan && Array.isArray(plan.steps) && plan.steps.length && todos.length) {
    const statusByContent: Record<string, "pending" | "in_progress" | "completed"> = {};
    for (const t of todos) statusByContent[t.content] = t.status;
    plan = {
      ...plan,
      steps: plan.steps.map((s) => {
        const content = typeof s === "string" ? s : String(s?.content ?? "");
        const status = typeof s === "string" ? "pending" : (s?.status ?? "pending");
        return { content, status: statusByContent[content] ?? status };
      }),
    };
  }
  return { feed, plan, todos, saved };
}

export function useAgent(): AgentState {
  const [connected, setConnected] = useState(false);
  const [crawlerId, setCrawlerId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<AgentSessionInfo[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [unread, setUnread] = useState<Record<string, boolean>>({});
  const [status, setStatus] = useState<AgentStatus>("idle");
  const [feed, setFeed] = useState<AgentFeedItem[]>([]);
  const [streaming, setStreaming] = useState("");
  const [plan, setPlan] = useState<AgentPlan | null>(null);
  const [todos, setTodos] = useState<AgentTodo[]>([]);
  const [questions, setQuestions] = useState<AgentQuestion[]>([]);
  const [saved, setSaved] = useState<AgentSavedItem[]>([]);
  const [login, setLogin] = useState<AgentLoginRequest | null>(null);
  const [qid, setQid] = useState<string | null>(null);
  const [editorCode, setEditorCodeState] = useState<string | null>(null);
  const [editorBase, setEditorBaseState] = useState<string | null>(null);
  const [turnSeq, setTurnSeq] = useState(0);

  const wsRef = useRef<WebSocket | null>(null);
  const crawlerIdRef = useRef<string | null>(null);
  const activeRef = useRef<string | null>(null);
  const unreadRefs = useRef<Record<string, boolean>>({});
  const sessionsRef = useRef<Record<string, AgentSessionInfo>>({});
  const feedsRef = useRef<Record<string, AgentFeedItem[]>>({});
  const streamingRefs = useRef<Record<string, string>>({});
  const statusRefs = useRef<Record<string, AgentStatus>>({});
  const plansRef = useRef<Record<string, AgentPlan | null>>({});
  const todosRefs = useRef<Record<string, AgentTodo[]>>({});
  const questionsRefs = useRef<Record<string, AgentQuestion[]>>({});
  const qidRefs = useRef<Record<string, string | null>>({});
  const savedRefs = useRef<Record<string, AgentSavedItem[]>>({});
  const loginRefs = useRef<Record<string, AgentLoginRequest | null>>({});
  const lastStatusRef = useRef<Record<string, string | null>>({});
  // 每个会话上一次 set_editor_code 写回的代码(跨轮次重置), 用于计算单次变更 diff
  const prevCodeBySessionRef = useRef<Record<string, string | null>>({});
  const editorCodeRef = useRef<string | null>(null);
  const editorBaseRef = useRef<string | null>(null);
  const turnSeqRef = useRef(0);
  const turnSeqBySessionRef = useRef<Record<string, number>>({});

  const initSessionRefs = useCallback((sid: string) => {
    if (feedsRef.current[sid] !== undefined) return;
    feedsRef.current[sid] = [];
    streamingRefs.current[sid] = "";
    statusRefs.current[sid] = "idle";
    plansRef.current[sid] = null;
    todosRefs.current[sid] = [];
    questionsRefs.current[sid] = [];
    qidRefs.current[sid] = null;
    savedRefs.current[sid] = [];
    loginRefs.current[sid] = null;
    lastStatusRef.current[sid] = null;
    prevCodeBySessionRef.current[sid] = null;
    if (turnSeqBySessionRef.current[sid] === undefined) {
      turnSeqBySessionRef.current[sid] = 0;
    }
  }, []);

  const setActiveStates = useCallback((sid: string) => {
    if (sid !== activeRef.current) return;
    setFeed(feedsRef.current[sid] ?? []);
    setStreaming(streamingRefs.current[sid] ?? "");
    setStatus(statusRefs.current[sid] ?? "idle");
    setPlan(plansRef.current[sid] ?? null);
    setTodos(todosRefs.current[sid] ?? []);
    setQuestions(questionsRefs.current[sid] ?? []);
    setSaved(savedRefs.current[sid] ?? []);
    setLogin(loginRefs.current[sid] ?? null);
    setQid(qidRefs.current[sid] ?? null);
    setTurnSeq(turnSeqBySessionRef.current[sid] ?? 0);
  }, []);

  const syncSessions = useCallback(() => {
    setSessions(Object.values(sessionsRef.current));
  }, []);

  const updateSessionInfo = useCallback((sid: string, patch: Partial<AgentSessionInfo>) => {
    const prev = sessionsRef.current[sid] ?? { id: sid, ...EMPTY_SESSION_INFO };
    sessionsRef.current = { ...sessionsRef.current, [sid]: { ...prev, ...patch } };
    syncSessions();
  }, [syncSessions]);

  const persistUnread = useCallback((cid: string | null) => {
    const key = UNREAD_KEY_PREFIX + (cid ?? "default");
    const map: Record<string, boolean> = {};
    for (const [sid, v] of Object.entries(unreadRefs.current)) {
      if (v) map[sid] = true;
    }
    saveJSON(key, map);
  }, []);

  const markSeen = useCallback((cid: string | null, sid: string, count: number) => {
    const key = SEEN_KEY_PREFIX + (cid ?? "default");
    const seen = loadJSON<Record<string, number>>(key, {});
    seen[sid] = count;
    saveJSON(key, seen);
  }, []);

  /** 会话产生新内容时更新未读状态: 正在查看的会话清除角标, 其余会话标记未读。 */
  const markActivity = useCallback((sid: string) => {
    if (!sid) return;
    const cid = crawlerIdRef.current;
    if (sid === activeRef.current) {
      if (unreadRefs.current[sid]) {
        unreadRefs.current = { ...unreadRefs.current, [sid]: false };
        setUnread(unreadRefs.current);
        persistUnread(cid);
      }
    } else {
      if (!unreadRefs.current[sid]) {
        unreadRefs.current = { ...unreadRefs.current, [sid]: true };
        setUnread(unreadRefs.current);
        persistUnread(cid);
      }
    }
  }, [persistUnread]);

  /** 前端第二层保障: 会话完成/停止后显式校正后端会话记录, 并回填最新消息数。 */
  const finalizeWithBackend = useCallback((sid: string, status: string) => {
    agentFinalize(sid, status, crawlerIdRef.current ?? undefined)
      .then((r) => {
        updateSessionInfo(sid, { message_count: r.message_count });
        markSeen(crawlerIdRef.current, sid, r.message_count);
      })
      .catch(() => {});
  }, [markSeen, updateSessionInfo]);

  /** 从服务端加载会话消息历史并重建 feed(仅在本地无 feed 时执行), 完成后恢复活动状态。 */
  const loadSessionFeed = useCallback(async (id: string) => {
    initSessionRefs(id);
    const current = feedsRef.current[id] ?? [];
    if (current.length > 0) {
      setActiveStates(id);
      return;
    }
    try {
      const { messages } = await agentGetMessages(id, crawlerIdRef.current ?? undefined);
      const built = buildFeedFromMessages(messages);
      let feedItems = built.feed;
      let planVal = built.plan;
      // 历史中的规划: 若该会话当前不在运行, 规划移入 feed 末尾(所属回答已在 feed 中),
      // 而不是留在输入区; 仅在运行中的会话才把规划放到输入区
      if (
        planVal &&
        statusRefs.current[id] !== "running" &&
        statusRefs.current[id] !== "waiting"
      ) {
        feedItems = [...feedItems, { kind: "plan", plan: planVal }];
        planVal = null;
      }
      feedsRef.current[id] = feedItems;
      plansRef.current[id] = planVal;
      todosRefs.current[id] = built.todos;
      savedRefs.current[id] = built.saved;
      if (statusRefs.current[id] === "idle" && built.feed.length) {
        statusRefs.current[id] = "done";
      }
      markSeen(crawlerIdRef.current, id, messages.length);
      // 拉取历史期间新到达的实时事件(运行中会话会持续推送), 追加到历史末尾避免被覆盖丢失
      const liveDuring = feedsRef.current[id] ?? [];
      if (liveDuring.length > 0) {
        feedsRef.current[id] = [...feedItems, ...liveDuring];
      }
    } catch {
      /* 忽略历史加载失败 */
    }
    setActiveStates(id);
  }, [initSessionRefs, setActiveStates, markSeen]);

  const pushToFeed = useCallback((sid: string, item: AgentFeedItem) => {
    initSessionRefs(sid);
    feedsRef.current[sid] = [...feedsRef.current[sid], item];
    if (sid === activeRef.current) setFeed(feedsRef.current[sid]);
  }, [initSessionRefs]);

  const applyToolResult = useCallback((
    sid: string,
    id: string | undefined,
    name: string | undefined,
    content: string,
    error: string,
  ) => {
    initSessionRefs(sid);
    const items = feedsRef.current[sid];
    let idx = -1;
    if (id) {
      idx = items.findIndex((it) => it.kind === "tool" && it.id === id);
    }
    if (idx === -1) {
      for (let i = items.length - 1; i >= 0; i--) {
        const it = items[i];
        if (it.kind === "tool" && it.name === name && (!it.state || it.state === "running")) {
          idx = i;
          break;
        }
      }
    }
    const patch = {
      state: error ? "error" : "done",
      content,
      error,
    } as const;
    if (idx === -1) {
      pushToFeed(sid, { kind: "tool", id, name, ...patch });
    } else {
      const next = items.slice();
      next[idx] = { ...next[idx], ...patch };
      feedsRef.current[sid] = next;
      if (sid === activeRef.current) setFeed(next);
    }
  }, [initSessionRefs, pushToFeed]);

  const flushStreaming = useCallback((sid: string, kind: "message" | "status" = "message") => {
    initSessionRefs(sid);
    const text = streamingRefs.current[sid] ?? "";
    if (text.trim()) {
      pushToFeed(sid, { kind, content: text });
    }
    streamingRefs.current[sid] = "";
    if (sid === activeRef.current) setStreaming("");
  }, [initSessionRefs, pushToFeed]);

  const setSessionStatus = useCallback((sid: string, st: AgentStatus) => {
    initSessionRefs(sid);
    statusRefs.current[sid] = st;
    if (sid === activeRef.current) setStatus(st);
    updateSessionInfo(sid, { status: st });
  }, [initSessionRefs, updateSessionInfo]);

  // 一轮结束: 把本轮规划以卡片形式放入 feed(位于所属回答上方), 并从输入区清除
  const finalizePlan = useCallback((sid: string) => {
    const p = plansRef.current[sid] ?? null;
    if (p) {
      pushToFeed(sid, { kind: "plan", plan: p });
    }
    plansRef.current[sid] = null;
    if (sid === activeRef.current) setPlan(null);
  }, [pushToFeed]);

  const removeSessionLocal = useCallback((sid: string) => {
    delete feedsRef.current[sid];
    delete streamingRefs.current[sid];
    delete statusRefs.current[sid];
    delete plansRef.current[sid];
    delete todosRefs.current[sid];
    delete questionsRefs.current[sid];
    delete qidRefs.current[sid];
    delete savedRefs.current[sid];
    delete loginRefs.current[sid];
    delete lastStatusRef.current[sid];
    delete prevCodeBySessionRef.current[sid];
    delete turnSeqBySessionRef.current[sid];
    if (unreadRefs.current[sid]) {
      delete unreadRefs.current[sid];
      setUnread(unreadRefs.current);
      persistUnread(crawlerIdRef.current);
    }
    const cid = crawlerIdRef.current;
    const seenKey = SEEN_KEY_PREFIX + (cid ?? "default");
    const seen = loadJSON<Record<string, number>>(seenKey, {});
    delete seen[sid];
    saveJSON(seenKey, seen);
    const next = { ...sessionsRef.current };
    delete next[sid];
    sessionsRef.current = next;
    syncSessions();
    if (activeRef.current === sid) {
      activeRef.current = null;
      setActiveId(null);
      setFeed([]);
      setStreaming("");
      setStatus("idle");
      setPlan(null);
      setTodos([]);
      setQuestions([]);
      setSaved([]);
      setLogin(null);
      setQid(null);
      setTurnSeq(0);
    }
  }, [persistUnread, syncSessions]);

  const selectSession = useCallback((id: string) => {
    if (activeRef.current === id) return;
    // 离开上一个会话: 记录其已看消息数(供刷新后未读判定)
    const prev = activeRef.current;
    if (prev) {
      markSeen(
        crawlerIdRef.current,
        prev,
        sessionsRef.current[prev]?.message_count ?? 0,
      );
    }
    activeRef.current = id;
    setActiveId(id);
    initSessionRefs(id);
    const info = sessionsRef.current[id];
    if (info) {
      statusRefs.current[id] = (info.status as AgentStatus) || "idle";
    }
    // 查看即清除未读角标
    unreadRefs.current = { ...unreadRefs.current, [id]: false };
    setUnread(unreadRefs.current);
    persistUnread(crawlerIdRef.current);
    void loadSessionFeed(id);
  }, [initSessionRefs, loadSessionFeed, persistUnread, markSeen]);

  const handleEvent = useCallback((msg: AgentEvent) => {
    const sid = msg.session_id ?? null;
    if (sid && ACTIVITY_TYPES.has(msg.type)) markActivity(sid);
    switch (msg.type) {
      case "hello": {
        if (msg.crawler_id) {
          crawlerIdRef.current = msg.crawler_id;
          setCrawlerId(msg.crawler_id);
        }
        const next: Record<string, AgentSessionInfo> = {};
        for (const s of msg.sessions ?? []) {
          // 会话标识统一用 session_id: 后端 session 文档同时带 _id(DB 主键)与 session_id,
          // 若误用 _id 会导致 hello 列表 key 与事件/接口的会话标识不一致, 出现重复空会话
          const sid = s.session_id ?? s.id;
          next[sid] = { ...s, id: sid };
          initSessionRefs(sid);
          if (s.plan) plansRef.current[sid] = s.plan;
        }
        sessionsRef.current = next;
        syncSessions();
        // 恢复未读状态: 消息数大于「上次查看时的计数」视为未读(覆盖离开期间后台完成/推进的会话)
        const cid = crawlerIdRef.current;
        const seen = loadJSON<Record<string, number>>(SEEN_KEY_PREFIX + (cid ?? "default"), {});
        const unreadInit: Record<string, boolean> = {};
        for (const s of msg.sessions ?? []) {
          const sid = s.session_id ?? s.id;
          if ((s.message_count ?? 0) > (seen[sid] ?? 0)) unreadInit[sid] = true;
        }
        unreadRefs.current = unreadInit;
        setUnread(unreadInit);
        persistUnread(cid);
        // 刷新后停在「新对话」界面: 不自动还原上次查看/运行中的会话。
        // 用户手动点开会话时由 selectSession 加载其历史与运行进度。
        break;
      }
      case "session_start": {
        if (!sid) break;
        initSessionRefs(sid);
        // 会话可能已存在(hello 已带服务端完整信息 / 事件缓冲回放)。回放场景不覆盖已有数据,
        // 避免把真实会话重置成「无内容」的空会话。
        const prev = sessionsRef.current[sid];
        const ts = Date.now();
        updateSessionInfo(sid, {
          id: sid,
          crawler_id: msg.crawler_id ?? crawlerIdRef.current ?? prev?.crawler_id ?? "",
          title: prev?.title ?? msg.title ?? "新会话",
          status: prev?.status ?? "idle",
          created_at: prev?.created_at ?? ts,
          updated_at: ts,
          message_count: prev?.message_count ?? 0,
          last_message: prev?.last_message ?? "",
        });
        break;
      }
      case "session_deleted": {
        if (!sid) break;
        removeSessionLocal(sid);
        break;
      }
      case "session_rename": {
        if (!sid) break;
        updateSessionInfo(sid, { title: msg.title ?? "" });
        break;
      }
      case "user_message": {
        if (!sid) break;
        initSessionRefs(sid);
        flushStreaming(sid);
        pushToFeed(sid, { kind: "user", content: msg.content ?? "" });
        setSessionStatus(sid, "running");
        updateSessionInfo(sid, { last_message: msg.content ?? "" });
        // 新一轮对话开始: 重置单次变更基线, 首个 editor_code 将以其 base 作为 diff 起点
        prevCodeBySessionRef.current[sid] = null;
        const seq = (turnSeqBySessionRef.current[sid] ?? 0) + 1;
        turnSeqBySessionRef.current[sid] = seq;
        if (sid === activeRef.current) {
          turnSeqRef.current = seq;
          setTurnSeq(seq);
        }
        break;
      }
      case "delta": {
        if (!sid) break;
        initSessionRefs(sid);
        streamingRefs.current[sid] = (streamingRefs.current[sid] ?? "") + (msg.content ?? "");
        if (sid === activeRef.current) setStreaming(streamingRefs.current[sid]);
        break;
      }
      case "message_end": {
        if (!sid) break;
        flushStreaming(sid);
        break;
      }
      case "tool": {
        if (!sid) break;
        if (msg.name && HIDDEN_TOOL_NAMES.has(msg.name)) break;
        flushStreaming(sid);
        pushToFeed(sid, {
          kind: "tool",
          id: msg.id,
          name: msg.name,
          args: msg.args,
          state: "running",
        });
        break;
      }
      case "tool_result": {
        if (!sid) break;
        if (msg.name && HIDDEN_TOOL_NAMES.has(msg.name)) break;
        applyToolResult(sid, msg.id, msg.name, msg.content ?? "", msg.error ?? "");
        break;
      }
      case "status": {
        if (!sid) break;
        initSessionRefs(sid);
        const c = msg.content ?? "";
        if (c && c !== lastStatusRef.current[sid]) {
          lastStatusRef.current[sid] = c;
          flushStreaming(sid);
          pushToFeed(sid, { kind: "status", content: c });
        }
        break;
      }
      case "plan": {
        if (!sid) break;
        flushStreaming(sid);
        plansRef.current[sid] = msg.plan ?? null;
        if (sid === activeRef.current) setPlan(plansRef.current[sid]);
        break;
      }
      case "todos": {
        if (!sid) break;
        flushStreaming(sid);
        todosRefs.current[sid] = msg.todos ?? [];
        if (sid === activeRef.current) setTodos(todosRefs.current[sid]);
        break;
      }
      case "saved": {
        if (!sid) break;
        const items = msg.saved ?? [];
        savedRefs.current[sid] = [...(savedRefs.current[sid] ?? []), ...items];
        if (sid === activeRef.current) setSaved(savedRefs.current[sid]);
        pushToFeed(sid, {
          kind: "saved",
          saved: items,
          content: `已保存 ${items.length} 项内容`,
        });
        break;
      }
      case "question": {
        if (!sid) break;
        flushStreaming(sid);
        questionsRefs.current[sid] = msg.questions ?? [];
        qidRefs.current[sid] = msg.qid ?? null;
        if (sid === activeRef.current) {
          setQuestions(questionsRefs.current[sid]);
          setQid(qidRefs.current[sid]);
        }
        setSessionStatus(sid, "waiting");
        break;
      }
      case "answer_received": {
        if (!sid) break;
        questionsRefs.current[sid] = [];
        qidRefs.current[sid] = null;
        if (sid === activeRef.current) {
          setQuestions([]);
          setQid(null);
        }
        setSessionStatus(sid, "running");
        break;
      }
      case "login_request": {
        if (!sid) break;
        flushStreaming(sid);
        const req: AgentLoginRequest = {
          qid: msg.qid ?? "",
          login_type: (msg.login_type as "qr" | "account") ?? "account",
          method: msg.method,
          url: msg.url,
          zoom_browser: msg.zoom_browser,
          message: msg.message,
          timeout: msg.timeout,
          fields: msg.fields,
          captcha: msg.captcha,
          submit_label: msg.submit_label,
        };
        loginRefs.current[sid] = req;
        if (sid === activeRef.current) setLogin(req);
        setSessionStatus(sid, "waiting");
        if (req.zoom_browser) {
          window.dispatchEvent(new Event("agent:login-zoom"));
        }
        break;
      }
      case "login_success": {
        if (!sid) break;
        flushStreaming(sid);
        loginRefs.current[sid] = null;
        if (sid === activeRef.current) {
          setLogin(null);
          window.dispatchEvent(new Event("agent:login-unzoom"));
        }
        const url = msg.url ? `，已跳转到 ${msg.url}` : "";
        pushToFeed(sid, { kind: "status", content: `登录成功${url}` });
        setSessionStatus(sid, "running");
        break;
      }
      case "login_action": {
        if (!sid) break;
        const text = msg.message ?? (msg.ok ? "操作成功" : "操作失败");
        pushToFeed(sid, { kind: "status", content: text });
        break;
      }
      case "editor_code": {
        editorCodeRef.current = msg.code ?? null;
        if (msg.base !== undefined) editorBaseRef.current = msg.base ?? null;
        setEditorCodeState(editorCodeRef.current);
        setEditorBaseState(editorBaseRef.current);
        if (!sid) break;
        initSessionRefs(sid);
        const prev = prevCodeBySessionRef.current[sid];
        const base = msg.base !== undefined ? (msg.base ?? "") : "";
        const from = prev !== null ? prev : base;
        const to = msg.code ?? "";
        if (from !== to) {
          pushToFeed(sid, { kind: "diff", diff: { from, to } });
        }
        prevCodeBySessionRef.current[sid] = to;
        break;
      }
      case "done": {
        if (!sid) break;
        flushStreaming(sid);
        // 任务完成: 规划卡片放到回答上方, 并从输入区清除
        finalizePlan(sid);
        if (msg.result) {
          const current = feedsRef.current[sid] ?? [];
          const last = current[current.length - 1];
          if (!(last && last.kind === "message" && last.content === msg.result)) {
            pushToFeed(sid, { kind: "message", content: msg.result });
          }
        }
        setSessionStatus(sid, "done");
        updateSessionInfo(sid, { last_message: msg.result ?? "" });
        finalizeWithBackend(sid, "done");
        break;
      }
      case "error": {
        if (!sid) break;
        flushStreaming(sid);
        finalizePlan(sid);
        pushToFeed(sid, { kind: "error", content: msg.error ?? "未知错误" });
        setSessionStatus(sid, "error");
        finalizeWithBackend(sid, "error");
        break;
      }
      case "session_end": {
        if (!sid) break;
        if (msg.reason === "cancelled") {
          finalizePlan(sid);
          setSessionStatus(sid, "cancelled");
          finalizeWithBackend(sid, "cancelled");
        } else if (msg.reason === "error") {
          finalizePlan(sid);
          setSessionStatus(sid, "error");
          finalizeWithBackend(sid, "error");
        } else if (statusRefs.current[sid] !== "error") {
          setSessionStatus(sid, "done");
          finalizeWithBackend(sid, "done");
        }
        lastStatusRef.current[sid] = null;
        break;
      }
      default:
        break;
    }
  }, [
    initSessionRefs,
    setActiveStates,
    syncSessions,
    updateSessionInfo,
    pushToFeed,
    flushStreaming,
    applyToolResult,
    setSessionStatus,
    finalizePlan,
    removeSessionLocal,
    markActivity,
    finalizeWithBackend,
    persistUnread,
  ]);

  useEffect(() => {
    let disposed = false;
    let ws: WebSocket | null = null;

    const connect = () => {
      if (disposed) return;
      const w = new WebSocket(agentWsUrl());
      wsRef.current = w;
      ws = w;
      w.onopen = () => {
        if (!disposed) setConnected(true);
      };
      w.onclose = () => {
        if (disposed) return;
        setConnected(false);
        setTimeout(connect, 2000);
      };
      w.onerror = () => w.close();
      w.onmessage = (ev) => {
        let msg: AgentEvent;
        try {
          msg = JSON.parse(ev.data as string) as AgentEvent;
        } catch {
          return;
        }
        if (msg.type === "ping") return;
        handleEvent(msg);
      };
    };

    connect();
    return () => {
      disposed = true;
      ws?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handleEvent]);

  // 进入空白新会话界面: 不创建会话, 直到用户发送消息时才 createSession
  const startNewSession = useCallback(() => {
    if (activeRef.current === null) return;
    markSeen(
      crawlerIdRef.current,
      activeRef.current,
      sessionsRef.current[activeRef.current]?.message_count ?? 0,
    );
    activeRef.current = null;
    setActiveId(null);
    setFeed([]);
    setStreaming("");
    setStatus("idle");
    setPlan(null);
    setTodos([]);
    setQuestions([]);
    setSaved([]);
    setLogin(null);
    setQid(null);
    setTurnSeq(0);
  }, [markSeen]);

  const createSession = useCallback(async () => {
    try {
      const r = await agentCreateSession(
        `新会话 · ${new Date().toLocaleTimeString()}`,
        crawlerIdRef.current ?? undefined,
      );
      const ts = Date.now();
      sessionsRef.current = {
        ...sessionsRef.current,
        [r.session_id]: {
          id: r.session_id,
          crawler_id: r.crawler_id,
          title: r.title,
          status: "idle",
          created_at: ts,
          updated_at: ts,
          message_count: 0,
          last_message: "",
        },
      };
      syncSessions();
      selectSession(r.session_id);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (activeRef.current) {
        pushToFeed(activeRef.current, { kind: "error", content: `新建会话失败: ${msg}` });
      }
    }
  }, [pushToFeed, selectSession, syncSessions]);

  const sendMessage = useCallback(async (content: string) => {
    const sid = activeRef.current;
    if (!sid) return;
    const st = statusRefs.current[sid];
    if (st === "running" || st === "waiting") return;
    try {
      await agentSendMessage(sid, content, crawlerIdRef.current ?? undefined);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      pushToFeed(sid, { kind: "error", content: `发送失败: ${msg}` });
    }
  }, [pushToFeed]);

  const submitAnswer = useCallback(async (answers: Record<string, unknown>) => {
    const sid = activeRef.current;
    const q = qidRefs.current[sid ?? ""];
    if (!sid || !q) return;
    try {
      await agentAnswer(sid, q, answers, crawlerIdRef.current ?? undefined);
    } catch (e) {
      pushToFeed(sid, {
        kind: "error",
        content: `提交答案失败: ${e instanceof Error ? e.message : String(e)}`,
      });
    }
  }, [pushToFeed]);

  const submitLogin = useCallback(async (answers: Record<string, unknown>) => {
    const sid = activeRef.current;
    const lg = loginRefs.current[sid ?? ""];
    if (!sid || !lg) return;
    try {
      await agentLoginAnswer(sid, lg.qid, answers, crawlerIdRef.current ?? undefined);
    } catch (e) {
      pushToFeed(sid, {
        kind: "error",
        content: `提交登录失败: ${e instanceof Error ? e.message : String(e)}`,
      });
    }
  }, [pushToFeed]);

  const sendLoginCode = useCallback(async () => {
    const sid = activeRef.current;
    if (!sid) return;
    try {
      await agentLoginAction(sid, "send_code", crawlerIdRef.current ?? undefined);
    } catch (e) {
      pushToFeed(sid, {
        kind: "error",
        content: `发送验证码失败: ${e instanceof Error ? e.message : String(e)}`,
      });
    }
  }, [pushToFeed]);

  const refreshCaptcha = useCallback(async () => {
    const sid = activeRef.current;
    if (!sid) return;
    try {
      const r = await agentLoginAction(sid, "refresh_captcha", crawlerIdRef.current ?? undefined);
      if (r.image && loginRefs.current[sid]) {
        const prev = loginRefs.current[sid];
        if (prev) {
          const captcha: AgentLoginCaptcha = { ...(prev.captcha ?? {}), type: prev.captcha?.type ?? "image", image: r.image };
          loginRefs.current[sid] = { ...prev, captcha };
          if (sid === activeRef.current) setLogin(loginRefs.current[sid]);
        }
      }
    } catch (e) {
      pushToFeed(sid, {
        kind: "error",
        content: `刷新验证码失败: ${e instanceof Error ? e.message : String(e)}`,
      });
    }
  }, [pushToFeed]);

  const cancelLogin = useCallback(async () => {
    const sid = activeRef.current;
    const lg = loginRefs.current[sid ?? ""];
    if (!sid || !lg) return;
    try {
      await agentLoginAnswer(sid, lg.qid, { cancelled: true }, crawlerIdRef.current ?? undefined);
    } catch (e) {
      pushToFeed(sid, {
        kind: "error",
        content: `取消登录失败: ${e instanceof Error ? e.message : String(e)}`,
      });
    }
  }, [pushToFeed]);

  const stop = useCallback(async () => {
    const sid = activeRef.current;
    if (!sid) return;
    const st = statusRefs.current[sid];
    if (st !== "running" && st !== "waiting") return;
    setSessionStatus(sid, "cancelled");
    try {
      await agentStop(sid, crawlerIdRef.current ?? undefined);
    } catch {
      /* ignore */
    }
    // 点击停止后的第二层保障: 显式校正会话记录
    finalizeWithBackend(sid, "cancelled");
  }, [finalizeWithBackend, setSessionStatus]);

  const deleteSession = useCallback(async (id: string) => {
    removeSessionLocal(id);
    try {
      await agentDeleteSession(id, crawlerIdRef.current ?? undefined);
    } catch (e) {
      pushToFeed(id, { kind: "error", content: `删除会话失败: ${e instanceof Error ? e.message : String(e)}` });
    }
  }, [pushToFeed, removeSessionLocal]);

  const renameSession = useCallback(async (id: string, title: string) => {
    try {
      const r = await agentRenameSession(id, title, crawlerIdRef.current ?? undefined);
      updateSessionInfo(id, { title: r.title });
    } catch (e) {
      pushToFeed(id, { kind: "error", content: `修改标题失败: ${e instanceof Error ? e.message : String(e)}` });
    }
  }, [pushToFeed, updateSessionInfo]);

  const activeSession = activeId ? (sessionsRef.current[activeId] ?? null) : null;

  return {
    connected,
    crawlerId,
    sessions,
    activeId,
    activeSession,
    status,
    unread,
    feed,
    streaming,
    plan,
    todos,
    questions,
    saved,
    login,
    qid,
    editorCode,
    editorBase,
    turnSeq,
    createSession,
    startNewSession,
    selectSession,
    sendMessage,
    submitAnswer,
    submitLogin,
    sendLoginCode,
    refreshCaptcha,
    cancelLogin,
    stop,
    deleteSession,
    renameSession,
  };
}
