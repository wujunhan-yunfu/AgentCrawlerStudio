import { useEffect, useRef, useState } from "react";
import * as monaco from "monaco-editor";
import type { ConsoleMessage } from "../hooks/useConsole";
import type { Problem } from "../hooks/useProblems";
import type { HighlightBox } from "./ElementsPanel";
import type { RunOutputLine, SavedItem } from "../utils/api";
import ApplicationPanel from "./ApplicationPanel";
import BrowserConsole from "./BrowserConsole";
import ElementsPanel from "./ElementsPanel";
import NetworkPanel from "./NetworkPanel";

interface Props {
  height: number;
  onResize: (height: number) => void;
  onCornerResizeStart: (e: React.MouseEvent<HTMLDivElement>) => void;
  running: boolean;
  onRun: () => void;
  onFormat: () => void;
  onOrganizeImports?: () => void;
  output: RunOutputLine[];
  pending: RunOutputLine | null;
  error: string;
  saved: SavedItem[];
  problems: Problem[];
  onJumpToProblem: (line: number, column: number) => void;
  consoleMessages: ConsoleMessage[];
  consoleConnected: boolean;
  onClearConsole: () => void;
  onEvaluate: (expression: string) => Promise<void>;
  onHighlight: (box: HighlightBox | null) => void;
}

const MIN_HEIGHT = 80;
const STATUSBAR_HEIGHT = 24;

const SEVERITY_LABEL = {
  [monaco.MarkerSeverity.Error]: "错误",
  [monaco.MarkerSeverity.Warning]: "警告",
  [monaco.MarkerSeverity.Info]: "信息",
  [monaco.MarkerSeverity.Hint]: "提示",
} as const;

export default function OutputBar({
  height,
  onResize,
  onCornerResizeStart,
  running,
  onRun,
  onFormat,
  onOrganizeImports,
  output,
  pending,
  error,
  saved,
  problems,
  onJumpToProblem,
  consoleMessages,
  consoleConnected,
  onClearConsole,
  onEvaluate,
  onHighlight,
}: Props) {
  const dragging = useRef(false);
  const outputRef = useRef<HTMLDivElement | null>(null);
  const [tab, setTab] = useState<"output" | "problems" | "devtools">("output");
  const [devSub, setDevSub] = useState<"console" | "elements" | "network" | "application">("console");
  const [menuOpen, setMenuOpen] = useState(false);
  const [expandedSaved, setExpandedSaved] = useState<Set<string>>(new Set());

  const toggleSaved = (id: string) => {
    setExpandedSaved((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const formatSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  };

  const formatTs = (ms: number): string => {
    const d = new Date(ms);
    const p = (n: number, len = 2) => String(n).padStart(len, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
  };

  const DEV_ITEMS: { key: typeof devSub; label: string }[] = [
    { key: "console", label: "控制台" },
    { key: "elements", label: "Elements" },
    { key: "network", label: "Network" },
    { key: "application", label: "Application" },
  ];
  const devLabel = DEV_ITEMS.find((i) => i.key === devSub)?.label ?? "控制台";

  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
  };

  useEffect(() => {
    const handleMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      const h = window.innerHeight - STATUSBAR_HEIGHT - e.clientY;
      onResize(Math.min(window.innerHeight - STATUSBAR_HEIGHT - 80, Math.max(MIN_HEIGHT, h)));
    };
    const handleUp = () => {
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
    };
  }, [onResize]);

  // 流式输出自动滚动到底部(与 Agent 面板一致), 实时跟随最新输出
  useEffect(() => {
    const el = outputRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [output, pending, error]);

  const errorCount = problems.filter((p) => p.severity === monaco.MarkerSeverity.Error).length;
  const warningCount = problems.filter((p) => p.severity === monaco.MarkerSeverity.Warning).length;
  const consoleErrorCount = consoleMessages.filter((m) => m.level === "error").length;
  const consoleWarnCount = consoleMessages.filter((m) => m.level === "warning").length;

  return (
    <section className="output-bar" style={{ height }}>
      <div className="resize-handle" title="拖拽调整高度" onMouseDown={handleMouseDown} />
      <div className="output-corner-handle" title="拖拽同时调整侧栏宽度与输出栏高度" onMouseDown={onCornerResizeStart} />
      <div className="output-bar-tabs">
        <button
          className={`tab ${tab === "output" ? "active" : ""}`}
          onClick={() => setTab("output")}
        >
          输出
        </button>
        <button
          className={`tab ${tab === "problems" ? "active" : ""}`}
          onClick={() => setTab("problems")}
        >
          问题
          {problems.length > 0 ? (
            <span className="problem-count">
              <span className="badge error">{errorCount}</span>
              <span className="badge warning">{warningCount}</span>
            </span>
          ) : null}
        </button>
        <div className="dev-menu-wrap">
          <button
            className={`tab ${tab === "devtools" ? "active" : ""}`}
            onClick={() => {
              setTab("devtools");
              setMenuOpen((o) => !o);
            }}
            title="浏览器控制台 (Elements / Console / Network / Application)"
          >
            浏览器控制台: {devLabel} <span className="dev-caret">▾</span>
            <span className="problem-count">
              <span className="badge error">{consoleErrorCount}</span>
              <span className="badge warning">{consoleWarnCount}</span>
            </span>
          </button>
          {menuOpen ? (
            <>
              <div className="dev-menu-backdrop" onClick={() => setMenuOpen(false)} />
              <div className="dev-menu">
                {DEV_ITEMS.map((it) => (
                  <button
                    key={it.key}
                    className={`dev-menu-item ${devSub === it.key ? "active" : ""}`}
                    onClick={() => {
                      setDevSub(it.key);
                      setTab("devtools");
                      setMenuOpen(false);
                    }}
                  >
                    {it.label}
                    {it.key === "console" ? (
                      <span className="problem-count">
                        <span className="badge error">{consoleErrorCount}</span>
                        <span className="badge warning">{consoleWarnCount}</span>
                      </span>
                    ) : null}
                  </button>
                ))}
              </div>
            </>
          ) : null}
        </div>
        <div className="output-bar-actions">
          {tab === "devtools" && devSub === "console" ? (
            <button onClick={onClearConsole} title="清空控制台">清空控制台</button>
          ) : null}
          <button onClick={onFormat} title="Shift+Alt+F">
            格式化代码
          </button>
          {onOrganizeImports ? (
            <button onClick={onOrganizeImports} title="Shift+Alt+O">
              整理导入
            </button>
          ) : null}
          <button className="primary" onClick={onRun} disabled={running}>
            {running ? "执行中..." : "执行代码"}
          </button>
        </div>
      </div>
      <div className="panel-stack">
        <div className={tab === "output" ? "panel-active" : "panel-hidden"}>
          <div className="output" ref={outputRef}>
            {output.length > 0 || pending ? (
              <div className="run-log">
                {output.map((line, i) => (
                  <div className="run-line" key={i}>
                    <span className="run-line-ts">{formatTs(line.ts)}</span>
                    <span className="run-line-text">{line.text}</span>
                  </div>
                ))}
                {pending ? (
                  <div className="run-line streaming">
                    <span className="run-line-ts">{formatTs(pending.ts)}</span>
                    <span className="run-line-text">
                      {pending.text}
                      {running ? <span className="caret" /> : null}
                    </span>
                  </div>
                ) : null}
              </div>
            ) : null}
            {error ? <pre className="err">{error}</pre> : null}
            {output.length === 0 && !pending && !error ? (
              <span className="hint">每次执行会自动重启全新无痕浏览器, 并提供 page / context / browser 对象。</span>
            ) : null}
          </div>
          {saved.length > 0 ? (
            <div className="saved-list">
              <div className="saved-head">已保存内容 ({saved.length})</div>
              {saved.map((item) => {
                const open = expandedSaved.has(item.id);
                return (
                  <div key={item.id} className="saved-item">
                    <button
                      className="saved-row"
                      onClick={() => toggleSaved(item.id)}
                      title="点击查看详情"
                    >
                      <span className={`saved-kind ${item.kind}`}>{item.kind === "page" ? "页面" : item.kind === "img" ? "图片" : "内容"}</span>
                      <span className="saved-name">{item.name}</span>
                      <span className="saved-size">{formatSize(item.size)}</span>
                      <span className="saved-caret">{open ? "▾" : "▸"}</span>
                    </button>
                    {open ? (
                      <div className="saved-detail">
                        <div className="saved-meta">路径: {item.path}</div>
                        <pre>{item.content}</pre>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
        <div className={tab === "devtools" && devSub === "console" ? "panel-active" : "panel-hidden"}>
          <BrowserConsole
            messages={consoleMessages}
            connected={consoleConnected}
            onClear={onClearConsole}
            onEvaluate={onEvaluate}
          />
        </div>
        <div className={tab === "devtools" && devSub === "elements" ? "panel-active" : "panel-hidden"}>
          <ElementsPanel onHighlight={onHighlight} />
        </div>
        <div className={tab === "devtools" && devSub === "network" ? "panel-active" : "panel-hidden"}>
          <NetworkPanel />
        </div>
        <div className={tab === "devtools" && devSub === "application" ? "panel-active" : "panel-hidden"}>
          <ApplicationPanel />
        </div>
        <div className={tab === "problems" ? "panel-active" : "panel-hidden"}>
          <div className="problems">
            {problems.length === 0 ? (
              <span className="hint">没有检测到问题。</span>
            ) : (
              <ul>
                {problems.map((p, i) => (
                  <li
                    key={i}
                    className={`problem sev-${p.severity}`}
                    onClick={() => onJumpToProblem(p.startLineNumber, p.startColumn)}
                    title="点击跳转到代码位置"
                  >
                    <span className="sev">{SEVERITY_LABEL[p.severity]}</span>
                    <span className="msg">{p.message}</span>
                    <span className="pos">
                      [{p.startLineNumber}, {p.startColumn}]
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
