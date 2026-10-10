import { useEffect, useRef, useState } from "react";
import * as monaco from "monaco-editor";
import { useI18n, type MessageKey } from "../i18n";
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
  onStop: () => void;
  onFormat: () => void;
  onOrganizeImports?: () => void;
  onExport?: () => void;
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

const SEVERITY_LABEL: Record<number, MessageKey> = {
  [monaco.MarkerSeverity.Error]: "output.sevError",
  [monaco.MarkerSeverity.Warning]: "output.sevWarning",
  [monaco.MarkerSeverity.Info]: "output.sevInfo",
  [monaco.MarkerSeverity.Hint]: "output.sevHint",
};

export default function OutputBar({
  height,
  onResize,
  onCornerResizeStart,
  running,
  onRun,
  onStop,
  onFormat,
  onOrganizeImports,
  onExport,
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
  const { t } = useI18n();
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

  const downloadSaved = (item: SavedItem) => {
    let blob: Blob;
    if (item.kind === "img") {
      const m = /^data:([^;,]+)(?:;[^,]*)?,([\s\S]*)$/.exec(item.content);
      const mime = m ? m[1] : "image/png";
      const bin = atob((m ? m[2] : item.content).trim());
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
      blob = new Blob([bytes], { type: mime });
    } else {
      blob = new Blob([item.content], { type: "text/plain;charset=utf-8" });
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = item.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const formatTs = (ms: number): string => {
    const d = new Date(ms);
    const p = (n: number, len = 2) => String(n).padStart(len, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
  };

  const formatDur = (ms: number): string => {
    if (ms < 1000) return `${ms}ms`;
    const s = ms / 1000;
    if (s < 60) return `${s.toFixed(2)}s`;
    const m = Math.floor(s / 60);
    return `${m}m${Math.round(s % 60)}s`;
  };

  const DEV_ITEMS: { key: typeof devSub; labelKey?: MessageKey; label?: string }[] = [
    { key: "console", labelKey: "dev.console" },
    { key: "elements", label: "Elements" },
    { key: "network", label: "Network" },
    { key: "application", label: "Application" },
  ];
  const devItemLabel = (it: (typeof DEV_ITEMS)[number]): string =>
    it.labelKey ? t(it.labelKey) : it.label ?? t("dev.console");
  const devLabel = devItemLabel(DEV_ITEMS.find((i) => i.key === devSub) ?? DEV_ITEMS[0]);

  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    document.body.classList.add("resizing");
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
      document.body.classList.remove("resizing");
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
      <div className="resize-handle" title={t("output.resizeHeight")} onMouseDown={handleMouseDown} />
      <div className="output-corner-handle" title={t("output.resizeCorner")} onMouseDown={onCornerResizeStart} />
      <div className="output-bar-tabs">
        <button
          className={`tab ${tab === "output" ? "active" : ""}`}
          onClick={() => setTab("output")}
        >
          {t("output.tabOutput")}
        </button>
        <button
          className={`tab ${tab === "problems" ? "active" : ""}`}
          onClick={() => setTab("problems")}
        >
          {t("output.tabProblems")}
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
            title={t("output.devtoolsTitle")}
          >
            {t("output.browserConsole", { label: devLabel })} <span className="dev-caret">▾</span>
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
                    {devItemLabel(it)}
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
            <button onClick={onClearConsole} title={t("output.clearConsole")}>{t("output.clearConsole")}</button>
          ) : null}
          <button onClick={onFormat} title="Shift+Alt+F">
            {t("output.format")}
          </button>
          {onOrganizeImports ? (
            <button onClick={onOrganizeImports} title="Shift+Alt+O">
              {t("output.organizeImports")}
            </button>
          ) : null}
          {onExport ? (
            <button onClick={onExport} title={t("output.exportTitle")}>
              {t("output.export")}
            </button>
          ) : null}
          <button
            className={`primary ${running ? "run-stop" : ""}`}
            onClick={running ? onStop : onRun}
            title={running ? t("output.stopRun") : undefined}
          >
            {running ? (
              <>
                <span className="run-stop-idle">{t("output.running")}</span>
                <span className="run-stop-hover">{t("output.stopRun")}</span>
              </>
            ) : (
              t("output.run")
            )}
          </button>
        </div>
      </div>
      <div className="panel-stack">
        <div className={tab === "output" ? "panel-active" : "panel-hidden"}>
          <div className="output" ref={outputRef}>
            {output.length > 0 || pending ? (
              <div className="run-log">
                {output.map((line, i) =>
                  line.kind === "marker" ? (
                    <div
                      className={`run-marker ${line.marker === "start" ? "start" : `end ${line.ok === false ? "fail" : "ok"}`}`}
                      key={i}
                    >
                      <span className="run-marker-line" />
                      <span className="run-marker-badge">
                        {line.marker === "start"
                          ? t("output.runStart")
                          : line.ok === false
                            ? t("output.runEndFail")
                            : t("output.runEndOk")}
                      </span>
                      {typeof line.dur === "number" && line.dur > 0 ? (
                        <span className="run-marker-dur">{formatDur(line.dur)}</span>
                      ) : null}
                      <span className="run-marker-ts">{formatTs(line.ts)}</span>
                      <span className="run-marker-line" />
                    </div>
                  ) : (
                    <div className="run-line" key={i}>
                      <span className="run-line-ts">{formatTs(line.ts)}</span>
                      <span className="run-line-text">{line.text}</span>
                    </div>
                  ),
                )}
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
              <span className="hint">{t("output.emptyHint")}</span>
            ) : null}
          </div>
          {saved.length > 0 ? (
            <div className="saved-list">
              <div className="saved-head">{t("output.savedHeader", { count: saved.length })}</div>
              {saved.map((item) => {
                const open = expandedSaved.has(item.id);
                return (
                  <div key={item.id} className="saved-item">
                    <div
                      className="saved-row"
                      role="button"
                      tabIndex={0}
                      onClick={() => toggleSaved(item.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          toggleSaved(item.id);
                        }
                      }}
                      title={t("output.savedDetailTitle")}
                    >
                      <span className={`saved-kind ${item.kind}`}>{item.kind === "page" ? t("output.kindPage") : item.kind === "img" ? t("output.kindImg") : t("output.kindContent")}</span>
                      <span className="saved-name">{item.name}</span>
                      <span className="saved-size">{formatSize(item.size)}</span>
                      <button
                        className="saved-download"
                        onClick={(e) => {
                          e.stopPropagation();
                          downloadSaved(item);
                        }}
                        title={t("output.downloadTitle")}
                      >
                        {t("output.download")}
                      </button>
                      <span className="saved-caret">{open ? "▾" : "▸"}</span>
                    </div>
                    {open ? (
                      <div className="saved-detail">
                        <div className="saved-meta">{t("output.path", { path: item.path })}</div>
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
              <span className="hint">{t("output.noProblems")}</span>
            ) : (
              <ul>
                {problems.map((p, i) => (
                  <li
                    key={i}
                    className={`problem sev-${p.severity}`}
                    onClick={() => onJumpToProblem(p.startLineNumber, p.startColumn)}
                    title={t("output.jumpProblem")}
                  >
                    <span className="sev">{t(SEVERITY_LABEL[p.severity])}</span>
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
