import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import type * as monaco from "monaco-editor";
import type { PanelKey } from "./types";
import type { HighlightBox } from "./components/ElementsPanel";
import ActivityBar from "./components/ActivityBar";
import CodeEditor from "./components/CodeEditor";
import LiveView from "./components/LiveView";
import OutputBar from "./components/OutputBar";
import Sidebar from "./components/Sidebar";
import { useLiveStream } from "./hooks/useLiveStream";
import { useRemoteControl } from "./hooks/useRemoteControl";
import { useConsole } from "./hooks/useConsole";
import { useProblems } from "./hooks/useProblems";
import { useStatus } from "./hooks/useStatus";
import { useVersions } from "./hooks/useVersions";
import { runCodeStream, organizeImports, setEditorCode, runLoginAnswer, runLoginAction, runLoginStatus, type RunOutputLine, type SavedItem, type RunLoginRequestData } from "./utils/api";

const OUTPUT_DEFAULT_HEIGHT = 300;
const ACTIVITY_BAR_WIDTH = 48;
const STATUSBAR_HEIGHT = 24;
const MIN_OUTPUT_HEIGHT = 80;
const SIDEBAR_DEFAULT_WIDTH = 280;
const SIDEBAR_AGENT_WIDTH = 460;
const COLLAPSE_THRESHOLD = 30;
const MIN_EDITOR_WIDTH = 120;

const PANEL_DEFAULT_WIDTH: Partial<Record<PanelKey, number>> = { agent: SIDEBAR_AGENT_WIDTH };

const DEFAULT_CODE = "";

export default function App() {
  const [code, setCode] = useState(DEFAULT_CODE);
  const [running, setRunning] = useState(false);
  const [output, setOutput] = useState<RunOutputLine[]>([]);
  const [pending, setPending] = useState<RunOutputLine | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState<SavedItem[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [runLogin, setRunLogin] = useState<RunLoginRequestData | null>(null);
  const [outputHeight, setOutputHeight] = useState(OUTPUT_DEFAULT_HEIGHT);
  const [sidebarWidth, setSidebarWidth] = useState(SIDEBAR_DEFAULT_WIDTH);
  const [highlight, setHighlight] = useState<HighlightBox | null>(null);
  const [activePanel, setActivePanel] = useState<PanelKey | null>(null);
  const activePanelRef = useRef<PanelKey | null>(null);
  const lastPanelRef = useRef<PanelKey | null>(null);
  const [liveMaximized, setLiveMaximized] = useState(false);
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const lastSyncedRef = useRef("");
  const codeRef = useRef(DEFAULT_CODE);
  const runAbortRef = useRef<AbortController | null>(null);
  const pendingOutputRef = useRef("");
  const [model, setModel] = useState<monaco.editor.ITextModel | null>(null);
  const problems = useProblems(model);
  const { imgRef, connected, lagMs, fps, width, height } = useLiveStream();
  const control = useRemoteControl();
  const consoleState = useConsole();
  const status = useStatus();
  const versions = useVersions(code, setCode);

  // 编辑器内容防抖同步到后端, 供"编码调试" Agent 读取/回写
  useEffect(() => {
    codeRef.current = code;
    const t = setTimeout(() => {
      if (code !== lastSyncedRef.current) {
        void setEditorCode(code).catch(() => {});
        lastSyncedRef.current = code;
      }
    }, 800);
    return () => clearTimeout(t);
  }, [code]);

  // Agent 每次 set_editor_code: 立即同步编辑器(差异由 Agent 面板按会话内联展示)
  const handleAgentCode = (agentCode: string) => {
    if (agentCode === codeRef.current) return;
    lastSyncedRef.current = agentCode;
    codeRef.current = agentCode;
    setCode(agentCode);
  };

  const handleFormat = () => {
    setError("");
    editorRef.current?.getAction("editor.action.formatDocument")?.run();
  };

  const handleOrganizeImports = () => {
    setError("");
    const editor = editorRef.current;
    const model = editor?.getModel();
    if (!editor || !model) return;
    const code = model.getValue();
    setRunning(true);
    organizeImports(code)
      .then((r) => {
        if (r.ok && r.formatted && r.formatted !== code) {
          model.pushEditOperations(
            [],
            [{ range: model.getFullModelRange(), text: r.formatted }],
            () => null,
          );
        } else if (r.error) {
          setError(r.error);
        }
      })
      .catch((e) => setError(`整理导入失败: ${e instanceof Error ? e.message : e}`))
      .finally(() => setRunning(false));
  };

  const handleJumpToProblem = (line: number, column: number) => {
    const editor = editorRef.current;
    if (!editor) return;
    editor.setPosition({ lineNumber: line, column });
    editor.revealPositionInCenter({ lineNumber: line, column });
    editor.focus();
  };

  // 流式输出: 把 stdout chunk 按行拆分, 每行附带产生该行的时间戳(类命令行日志);
  // 未以换行结尾的半个行作为 pending 实时渲染(带闪烁光标), 换行后落入已提交行列表
  const appendOutput = (data: string, ts: number) => {
    const text = pendingOutputRef.current + data;
    const parts = text.split("\n");
    const complete = parts.slice(0, -1);
    pendingOutputRef.current = parts[parts.length - 1];
    if (complete.length) {
      setOutput((prev) => [...prev, ...complete.map((t) => ({ ts, text: t }))]);
    }
    setPending(pendingOutputRef.current ? { ts, text: pendingOutputRef.current } : null);
  };

  const handleStop = () => {
    runAbortRef.current?.abort();
  };

  const handleRun = async () => {
    if (running) return;
    const abort = new AbortController();
    runAbortRef.current = abort;
    setRunning(true);
    setOutput([]);
    pendingOutputRef.current = "";
    setPending(null);
    setError("");
    setSaved([]);
    setRunId(null);
    setRunLogin(null);
    const id = `run_${Date.now().toString(36)}`;
    setRunId(id);
    void pollRunLogin(id, abort.signal);

    // 冲刷未换行的半个输出行, 保证标记与输出顺序正确
    const flushPending = (ts: number) => {
      if (pendingOutputRef.current) {
        setOutput((prev) => [...prev, { ts, text: pendingOutputRef.current }]);
        pendingOutputRef.current = "";
        setPending(null);
      }
    };
    // 样式化执行开始/结束标记(非文本)
    const addMarker = (m: { marker: "start" | "end"; ts: number; ok?: boolean; dur?: number }) => {
      setOutput((prev) => [...prev, { kind: "marker", text: "", ...m }]);
    };

    let runStartTs = 0;
    try {
      const r = await runCodeStream(code, id, (chunk) => {
        if (chunk.type === "start") {
          runStartTs = chunk.ts;
          addMarker({ marker: "start", ts: chunk.ts });
        } else if (chunk.type === "stdout") {
          appendOutput(chunk.data, chunk.ts);
        } else if (chunk.type === "done") {
          flushPending(chunk.ts);
          addMarker({
            marker: "end",
            ts: chunk.ts,
            ok: chunk.result.ok,
            dur: runStartTs ? chunk.ts - runStartTs : 0,
          });
        }
      }, abort.signal);
      flushPending(Date.now());
      setError(r.error);
      setSaved(r.saved ?? []);
    } catch (e) {
      flushPending(Date.now());
      addMarker({ marker: "end", ts: Date.now(), ok: false });
      const aborted = e instanceof DOMException && e.name === "AbortError";
      setError(aborted ? "执行已停止" : `请求失败: ${e instanceof Error ? e.message : e}`);
    } finally {
      setRunning(false);
      setRunId(null);
      setRunLogin(null);
      setLiveMaximized(false);
      runAbortRef.current = null;
    }
  };

  // 独立运行期间轮询登录请求: 脚本调用 page_login 时挂起, 前端据此展示登录框
  const pollRunLogin = async (id: string, signal: AbortSignal) => {
    try {
      while (!signal.aborted) {
        const r = await runLoginStatus(id);
        if (r.waiting && r.request) {
          setRunLogin(r.request);
          if (r.request.zoom_browser) setLiveMaximized(true);
        }
        await new Promise((res) => setTimeout(res, 1500));
      }
    } catch {
      /* 运行结束或后端不可用, 停止轮询 */
    }
  };

  const handleRunLoginSubmit = async (answers: Record<string, unknown>) => {
    if (!runId) return;
    try {
      await runLoginAnswer(runId, answers);
      setRunLogin(null);
      if (runLogin?.zoom_browser) setLiveMaximized(false);
    } catch (e) {
      setError(`提交登录失败: ${e instanceof Error ? e.message : e}`);
    }
  };

  const handleRunLoginAction = async (action: string) => {
    if (!runId) return;
    try {
      const r = await runLoginAction(runId, action);
      if ((action === "refresh_captcha" || action === "refresh_qr") && r.message && !r.ok) {
        setError(r.message);
      }
    } catch (e) {
      setError(`登录动作失败: ${e instanceof Error ? e.message : e}`);
    }
  };

  const handleSelect = (key: PanelKey) => {
    if (key === "code") {
      setActivePanel(null);
      return;
    }
    setActivePanel((p) => (p === key ? null : key));
  };

  // 记录最近打开的面板; 收起后拖拽边线可重新展开该面板; 面板打开时若宽度为 0 使用面板默认宽度
  useEffect(() => {
    activePanelRef.current = activePanel;
    if (activePanel) {
      lastPanelRef.current = activePanel;
      if (sidebarWidth === 0) {
        setSidebarWidth(PANEL_DEFAULT_WIDTH[activePanel] ?? SIDEBAR_DEFAULT_WIDTH);
      }
    }
  }, [activePanel]);

  const beginSidebarResize = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    const collapsed = activePanelRef.current === null;
    const startX = e.clientX;
    const startWidth = collapsed ? 0 : sidebarWidth;
    let isCollapsed = collapsed;
    const maxW = Math.max(MIN_EDITOR_WIDTH, window.innerWidth - ACTIVITY_BAR_WIDTH - MIN_EDITOR_WIDTH);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (ev: MouseEvent) => {
      const w = Math.max(0, Math.min(maxW, startWidth + (ev.clientX - startX)));
      if (w > COLLAPSE_THRESHOLD) {
        if (isCollapsed) {
          isCollapsed = false;
          setActivePanel(lastPanelRef.current ?? "browser");
        }
        setSidebarWidth(w);
      } else {
        if (!isCollapsed) {
          isCollapsed = true;
          setActivePanel(null);
        }
        setSidebarWidth(0);
      }
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

  // 输出栏左上角与左栏边线的交汇处: 一个拖拽同时调整侧栏宽度与输出栏高度
  const beginCornerResize = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    const collapsed = activePanelRef.current === null;
    const startX = e.clientX;
    const startY = e.clientY;
    const startWidth = collapsed ? 0 : sidebarWidth;
    const startHeight = outputHeight;
    let isCollapsed = collapsed;
    const maxW = Math.max(MIN_EDITOR_WIDTH, window.innerWidth - ACTIVITY_BAR_WIDTH - MIN_EDITOR_WIDTH);
    const maxH = Math.max(MIN_OUTPUT_HEIGHT, window.innerHeight - STATUSBAR_HEIGHT - MIN_OUTPUT_HEIGHT);
    document.body.style.cursor = "nwse-resize";
    document.body.style.userSelect = "none";

    const onMove = (ev: MouseEvent) => {
      const w = Math.max(0, Math.min(maxW, startWidth + (ev.clientX - startX)));
      const h = Math.max(MIN_OUTPUT_HEIGHT, Math.min(maxH, startHeight + (startY - ev.clientY)));
      if (w > COLLAPSE_THRESHOLD) {
        if (isCollapsed) {
          isCollapsed = false;
          setActivePanel(lastPanelRef.current ?? "browser");
        }
        setSidebarWidth(w);
      } else {
        if (!isCollapsed) {
          isCollapsed = true;
          setActivePanel(null);
        }
        setSidebarWidth(0);
      }
      setOutputHeight(h);
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

  // page_login 登录引导: 放大/还原实时画面(便于用户扫码或核对页面)
  useEffect(() => {
    const onZoom = () => setLiveMaximized(true);
    const onUnzoom = () => setLiveMaximized(false);
    window.addEventListener("agent:login-zoom", onZoom);
    window.addEventListener("agent:login-unzoom", onUnzoom);
    return () => {
      window.removeEventListener("agent:login-zoom", onZoom);
      window.removeEventListener("agent:login-unzoom", onUnzoom);
    };
  }, []);

  const hasBrowser = Boolean(status?.chrome);

  return (
    <div className="vscode-layout" style={{ "--output-h": `${outputHeight}px` } as CSSProperties}>
      <ActivityBar active={activePanel} onSelect={handleSelect} />
      <Sidebar
        panel={activePanel}
        onClose={() => setActivePanel(null)}
        status={status}
        onAgentCode={handleAgentCode}
        versions={versions}
        width={sidebarWidth}
        onResizeStart={beginSidebarResize}
      />
      <main className="editor-area">
        <CodeEditor
          value={code}
          onChange={setCode}
          onRun={() => void handleRun()}
          onFormatError={setError}
          onEditorReady={(e) => {
            editorRef.current = e;
            setModel(e.getModel());
          }}
        />
      </main>
      <OutputBar
        height={outputHeight}
        onResize={setOutputHeight}
        onCornerResizeStart={beginCornerResize}
        running={running}
        onRun={() => void handleRun()}
        onStop={handleStop}
        onFormat={handleFormat}
        onOrganizeImports={handleOrganizeImports}
        output={output}
        pending={pending}
        error={error}
        saved={saved}
        problems={problems}
        onJumpToProblem={handleJumpToProblem}
        consoleMessages={consoleState.messages}
        consoleConnected={consoleState.connected}
        onClearConsole={consoleState.clear}
        onEvaluate={consoleState.evaluate}
        onHighlight={setHighlight}
      />
      <footer className="statusbar">
        <span className={status?.xvfb ? "ok" : "bad"}>Xvfb ●</span>
        <span className={status?.chrome ? "ok" : "bad"}>Chrome ●</span>
        <span>抓屏 {status?.capture?.fps ?? "-"} fps</span>
        <span>观看 {status?.capture?.viewers ?? "-"}</span>
        <span style={{ marginLeft: "auto" }}>末帧延迟 {status?.capture?.last_frame_age_ms != null ? `${status.capture.last_frame_age_ms} ms` : "-"}</span>
      </footer>
      <LiveView
        imgRef={imgRef}
        connected={connected}
        lagMs={lagMs}
        fps={fps}
        hasBrowser={hasBrowser}
        connecting={status === null}
        width={width}
        height={height}
        highlight={highlight}
        maximized={liveMaximized}
        onToggle={() => setLiveMaximized((m) => !m)}
        control={control}
        running={running}
      />
      {runLogin ? (
        <RunLoginModal
          login={runLogin}
          onSubmit={(a) => void handleRunLoginSubmit(a)}
          onCancel={() => void handleRunLoginSubmit({ cancelled: true })}
          onSendCode={() => void handleRunLoginAction("send_code")}
          onRefreshCaptcha={() => void handleRunLoginAction("refresh_captcha")}
          onRefreshQr={() => void handleRunLoginAction("refresh_qr")}
        />
      ) : null}
    </div>
  );
}

function RunLoginModal({
  login,
  onSubmit,
  onCancel,
  onSendCode,
  onRefreshCaptcha,
  onRefreshQr,
}: {
  login: RunLoginRequestData;
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

  const submit = (answers: Record<string, unknown>) => {
    setBusy(true);
    onSubmit(answers);
    setTimeout(() => setBusy(false), 500);
  };

  if (login.login_type === "qr") {
    return (
      <div className="run-login-overlay">
        <div className="run-login-card">
          <div className="run-login-title">🔐 二维码登录</div>
          <div className="run-login-hint">
            {login.message ?? "请在放大后的浏览器实时画面中用手机 APP 扫码登录。"}
          </div>
          <div className="run-login-hint sub">系统已放大浏览器实时画面并持续监听登录跳转，扫码成功后脚本会自动继续。</div>
          <div className="run-login-actions">
            <button className="primary" disabled={busy} onClick={() => submit({ ok: true })}>
              我已经完成扫码，继续
            </button>
            <button onClick={onRefreshQr}>刷新二维码</button>
            <button onClick={onCancel}>取消登录</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="run-login-overlay">
      <div className="run-login-card">
        <div className="run-login-title">🔐 账号登录</div>
        {(login.fields ?? []).map((f) => (
          <div key={f.key} className="run-login-field">
            <div className="run-login-label">{f.label}</div>
            <input
              type={f.input_type === "password" ? "password" : "text"}
              value={(values[f.key] as string) ?? ""}
              placeholder={f.placeholder || `请输入${f.label}`}
              onChange={(e) => setVal(f.key, e.target.value)}
            />
          </div>
        ))}
        {login.captcha && login.captcha.type !== "none" ? (
          <div className="run-login-field">
            <div className="run-login-label">验证码</div>
            <div className="run-login-captcha-row">
              <input
                type="text"
                value={(values.captcha as string) ?? ""}
                placeholder="请输入验证码"
                onChange={(e) => setVal("captcha", e.target.value)}
              />
              {login.captcha.type === "image" && login.captcha.image ? (
                <img
                  className="run-login-captcha-img"
                  src={login.captcha.image}
                  alt="验证码"
                  title="点击刷新"
                  onClick={onRefreshCaptcha}
                />
              ) : null}
            </div>
            {login.captcha.type === "sms" ? (
              <button className="run-login-send-code" disabled={countdown > 0} onClick={() => { setCountdown(60); onSendCode(); }}>
                {countdown > 0 ? `${countdown}s 后重发` : "发送验证码"}
              </button>
            ) : null}
            {login.captcha.type === "image" ? (
              <button className="run-login-send-code" onClick={onRefreshCaptcha}>换一张</button>
            ) : null}
          </div>
        ) : null}
        <div className="run-login-actions">
          <button className="primary" disabled={busy} onClick={() => submit(values)}>
            {login.submit_label ? `提交${login.submit_label}` : "提交登录"}
          </button>
          <button onClick={onCancel}>取消登录</button>
        </div>
      </div>
    </div>
  );
}
