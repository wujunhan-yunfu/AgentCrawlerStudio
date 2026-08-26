export type PanelKey = "code" | "browser" | "status" | "pages" | "tools" | "agent" | "versions";

export interface PageInfo {
  id: string;
  url: string;
  title: string;
}

export interface CaptureStatus {
  running: boolean;
  error: string | null;
  viewers: number;
  fps: number | null;
  frames_total: number;
  last_frame_age_ms: number | null;
}

export interface ConsoleStatus {
  targets: number;
  connections: number;
  subscribers: Record<string, number>;
  history: number;
}

export interface Status {
  uptime: number | null;
  error: string | null;
  xvfb: boolean;
  chrome: boolean;
  chrome_cdp: string;
  capture: CaptureStatus;
  cdp: ConsoleStatus | null;
  pages: PageInfo[];
}

/* ---------------- 爬虫 Agent ---------------- */

export type AgentQuestionType = "single" | "multi" | "text";

export interface AgentQuestion {
  key: string;
  title: string;
  type: AgentQuestionType;
  options?: string[];
  default?: string;
}

export interface AgentLoginField {
  key: string;
  label: string;
  input_type: string;
  selector?: string;
  placeholder?: string;
}

export interface AgentLoginCaptcha {
  type: "none" | "sms" | "image";
  input_key?: string;
  input_selector?: string;
  send_selector?: string;
  image_selector?: string;
  image?: string;
}

export interface AgentLoginRequest {
  qid: string;
  login_type: "qr" | "account";
  method?: string;
  url?: string;
  zoom_browser?: boolean;
  message?: string;
  timeout?: number;
  fields?: AgentLoginField[];
  captcha?: AgentLoginCaptcha;
  submit_label?: string;
}

export type AgentPlanStep = string | { content: string; status: "pending" | "in_progress" | "completed" };

export interface AgentPlan {
  goal?: string;
  candidate_sites?: string[];
  scope?: string;
  method?: string;
  login_required?: boolean;
  data_fields?: string[];
  steps?: AgentPlanStep[];
  [key: string]: unknown;
}

export interface AgentTodo {
  content: string;
  status: "pending" | "in_progress" | "completed";
}

export interface AgentSavedItem {
  id: string;
  kind?: string;
  name: string;
  path: string;
  size: number;
  content?: string;
}

export type AgentStatus = "idle" | "running" | "waiting" | "done" | "error" | "cancelled";

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
  plan?: AgentPlan | null;
  question?: { qid?: string; questions?: AgentQuestion[] } | null;
}

export interface AgentStoredMessage {
  id: string;
  session_id: string;
  crawler_id: string;
  role: "user" | "assistant" | "event";
  type: string;
  content: string;
  meta?: {
    plan?: AgentPlan;
    todos?: AgentTodo[];
    saved?: AgentSavedItem[];
    questions?: AgentQuestion[];
    qid?: string;
    [key: string]: unknown;
  };
  ts: number;
}

export interface AgentToolEvent {
  name: string;
  args: string;
  id?: string;
}

export interface AgentToolResultEvent {
  name: string;
  content: string;
  error: string;
}

export interface AgentFeedItem {
  kind: "user" | "message" | "tool" | "tool_result" | "status" | "error" | "saved" | "answer" | "diff" | "plan";
  content?: string;
  name?: string;
  args?: string;
  error?: string;
  saved?: AgentSavedItem[];
  id?: string;
  state?: "running" | "done" | "error";
  /** 一次 set_editor_code 的代码变更: 变更前 -> 变更后 */
  diff?: { from?: string; to?: string };
  /** 该轮对话产生的爬取规划(任务完成后展示在所属 AGENT 回答上方) */
  plan?: AgentPlan | null;
}
