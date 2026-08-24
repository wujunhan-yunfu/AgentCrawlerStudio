export const API_PREFIX = "/api/v1";

export function api(path: string): string {
  return `${API_PREFIX}${path}`;
}

export interface SavedItem {
  id: string;
  kind: "page" | "content" | "img";
  name: string;
  path: string;
  size: number;
  content: string;
}

export interface RunResult {
  ok: boolean;
  output: string;
  error: string;
  saved: SavedItem[];
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(api(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  const j = (await r.json()) as { detail?: string };
  if (!r.ok) {
    throw new Error(j?.detail || r.statusText);
  }
  return j as T;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(api(path));
  const j = (await r.json()) as { detail?: string };
  if (!r.ok) {
    throw new Error(j?.detail || r.statusText);
  }
  return j as T;
}

async function del<T>(path: string): Promise<T> {
  const r = await fetch(api(path), { method: "DELETE" });
  const j = (await r.json()) as { detail?: string };
  if (!r.ok) {
    throw new Error(j?.detail || r.statusText);
  }
  return j as T;
}

async function patch<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(api(path), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  const j = (await r.json()) as { detail?: string };
  if (!r.ok) {
    throw new Error(j?.detail || r.statusText);
  }
  return j as T;
}

export function runCode(code: string, runId?: string): Promise<RunResult> {
  return post<RunResult>("/run", { code, run_id: runId });
}

export interface RunLoginField {
  key: string;
  label: string;
  input_type: string;
  placeholder?: string;
}

export interface RunLoginCaptcha {
  type: "none" | "sms" | "image";
  input_key?: string;
  input_selector?: string;
  send_selector?: string;
  image_selector?: string;
  image?: string;
}

export interface RunLoginRequestData {
  qid: string;
  login_type: "qr" | "account";
  method?: string;
  url?: string;
  zoom_browser?: boolean;
  message?: string;
  timeout?: number;
  fields?: RunLoginField[];
  captcha?: RunLoginCaptcha;
  submit_label?: string;
}

export interface RunLoginResult {
  run_id: string;
  waiting: boolean;
  request: RunLoginRequestData | null;
}

export function runLoginStatus(runId: string): Promise<RunLoginResult> {
  return get(`/run/${runId}/login`);
}

export function runLoginAnswer(runId: string, answers: Record<string, unknown>): Promise<{ ok: boolean }> {
  return post(`/run/${runId}/login-answer`, { answers });
}

export function runLoginAction(runId: string, action: string): Promise<{ ok: boolean; message?: string }> {
  return post(`/run/${runId}/login-action`, { action });
}

export interface FormatResult {
  ok: boolean;
  formatted: string;
  error: string;
}

export function organizeImports(code: string): Promise<FormatResult> {
  return post<FormatResult>("/organize-imports", { code });
}

export function formatCode(code: string): Promise<FormatResult> {
  return post<FormatResult>("/format", { code });
}

export function navigate(url: string, newPage: boolean): Promise<unknown> {
  return post("/navigate", { url, new_page: newPage });
}

export function restart(): Promise<unknown> {
  return post("/restart");
}

export async function screenshotBlob(): Promise<Blob> {
  const r = await fetch(api("/screenshot"), { method: "POST" });
  if (!r.ok) {
    throw new Error(`截图失败: ${r.status}`);
  }
  return r.blob();
}

/* ---------------- 爬虫 Agent ---------------- */

export interface AgentSessionInfo {
  id: string;
  session_id?: string;
  crawler_id: string;
  title: string;
  status: string;
  created_at: number;
  updated_at: number;
  message_count: number;
  last_message: string;
  plan?: unknown;
  question?: unknown;
}

export interface AgentStoredMessage {
  id: string;
  session_id: string;
  crawler_id: string;
  role: "user" | "assistant" | "event";
  type: string;
  content: string;
  meta?: Record<string, unknown>;
  ts: number;
}

function qs(params: Record<string, string | undefined>): string {
  const q = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  if (!q.length) return "";
  return `?${new URLSearchParams(q as [string, string][]).toString()}`;
}

export function agentInfo(): Promise<{ crawler_id: string }> {
  return get("/agent/info");
}

export function agentCreateSession(
  title: string,
  crawlerId?: string,
): Promise<{ session_id: string; crawler_id: string; title: string; status: string }> {
  return post("/agent/session", { title, crawler_id: crawlerId });
}

export function agentListSessions(crawlerId?: string): Promise<{ sessions: AgentSessionInfo[] }> {
  return get(`/agent/sessions${qs({ crawler_id: crawlerId })}`);
}

export function agentGetMessages(
  sessionId: string,
  crawlerId?: string,
): Promise<{ session_id: string; messages: AgentStoredMessage[] }> {
  return get(`/agent/session/${sessionId}/messages${qs({ crawler_id: crawlerId })}`);
}

export function agentSendMessage(
  sessionId: string,
  content: string,
  crawlerId?: string,
): Promise<{ session_id: string; status: string }> {
  return post(`/agent/session/${sessionId}/message`, { content, crawler_id: crawlerId });
}

export function agentDeleteSession(sessionId: string, crawlerId?: string): Promise<{ ok: boolean }> {
  return del(`/agent/session/${sessionId}${qs({ crawler_id: crawlerId })}`);
}

export function agentRenameSession(
  sessionId: string,
  title: string,
  crawlerId?: string,
): Promise<{ ok: boolean; session_id: string; title: string }> {
  return patch(`/agent/session/${sessionId}`, { title, crawler_id: crawlerId });
}

export function agentStart(task: string): Promise<{ session_id: string; status: string }> {
  return post("/agent/start", { task });
}

export function agentAnswer(
  sessionId: string,
  qid: string,
  answers: Record<string, unknown>,
  crawlerId?: string,
): Promise<{ ok: boolean }> {
  return post("/agent/answer", { session_id: sessionId, qid, answers, crawler_id: crawlerId });
}

export function agentLoginAction(
  sessionId: string,
  action: string,
  crawlerId?: string,
): Promise<{ ok: boolean; message?: string; image?: string }> {
  return post("/agent/login-action", { session_id: sessionId, action, crawler_id: crawlerId });
}

export function agentLoginAnswer(
  sessionId: string,
  qid: string,
  answers: Record<string, unknown>,
  crawlerId?: string,
): Promise<{ ok: boolean }> {
  return post("/agent/login-answer", { session_id: sessionId, qid, answers, crawler_id: crawlerId });
}

export function agentStop(sessionId: string, crawlerId?: string): Promise<{ ok: boolean }> {
  return post("/agent/stop", { session_id: sessionId, crawler_id: crawlerId });
}

/** 前端第二层保障: 会话完成/停止后显式校正会话记录(message_count/last_message/status)。 */
export function agentFinalize(
  sessionId: string,
  status?: string,
  crawlerId?: string,
): Promise<{ ok: boolean; session_id: string; status: string; message_count: number }> {
  return post(`/agent/session/${sessionId}/finalize`, { status, crawler_id: crawlerId });
}

export function getEditorCode(): Promise<{ ok: boolean; code: string }> {
  return post("/editor/code");
}

export function setEditorCode(code: string): Promise<{ ok: boolean }> {
  return post("/editor/code", { code });
}

export function agentWsUrl(): string {
  const url = api("/ws/agent");
  return url.replace(/^http/, "ws");
}
