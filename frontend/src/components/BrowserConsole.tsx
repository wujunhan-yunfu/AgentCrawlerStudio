import { useEffect, useMemo, useRef, useState } from "react";
import type { ConsoleMessage } from "../hooks/useConsole";
import ObjectTree from "./ObjectTree";

interface Props {
  messages: ConsoleMessage[];
  connected: boolean;
  onClear: () => void;
  onEvaluate: (expression: string) => Promise<void>;
}

const LEVEL_RANK: Record<string, number> = {
  debug: 0,
  verbose: 0,
  log: 1,
  info: 1,
  input: 1,
  warning: 2,
  error: 3,
};

const FILTERS = [
  { key: "all", label: "所有级别", rank: -1 },
  { key: "verbose", label: "详细", rank: 0 },
  { key: "info", label: "信息", rank: 1 },
  { key: "warning", label: "警告", rank: 2 },
  { key: "error", label: "错误", rank: 3 },
];

function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

function shortUrl(url: string): string {
  try {
    const u = new URL(url);
    return `${u.host}${u.pathname}${u.search}`;
  } catch {
    return url.length > 40 ? url.slice(-40) : url;
  }
}

function cellText(v: unknown): string {
  if (v === null) return "null";
  if (v === undefined) return "undefined";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }
  return String(v);
}

function Table({ data }: { data: unknown }) {
  if (!Array.isArray(data) || data.length === 0) {
    return <span className="cplain">(空表)</span>;
  }
  const first = data[0];
  const cols =
    typeof first === "object" && first !== null
      ? Object.keys(first as object)
      : ["(index)"];
  return (
    <table className="ctable">
      <thead>
        <tr>
          <th>(index)</th>
          {cols.map((c) => (
            <th key={c}>{c}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.map((row, i) => (
          <tr key={i}>
            <td className="ctable-index">{i}</td>
            {cols.map((c) => {
              const v =
                typeof row === "object" && row !== null
                  ? (row as Record<string, unknown>)[c]
                  : row;
              return <td key={c}>{cellText(v)}</td>;
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ConsoleRow({ msg, timestamps }: { msg: ConsoleMessage; timestamps: boolean }) {
  const indent = (msg.group ?? 0) * 18;
  const isInput = msg.kind === "eval-input";
  return (
    <div
      className={`clog lv-${msg.level} ${isInput ? "is-input" : ""}`}
      style={{ paddingLeft: 10 + indent }}
    >
      {timestamps ? <span className="clog-ts">{fmtTime(msg.ts)}</span> : null}
      <span className="clog-arrow">›</span>
      <span className="clog-body">
        {msg.kind === "table" && msg.table !== undefined && msg.table !== null ? (
          <Table data={msg.table} />
        ) : (
          <span className="clog-segs">
            {(msg.items ?? []).map((it, j) => (
              <ObjectTree key={j} item={it} />
            ))}
          </span>
        )}
        {msg.stack ? <span className="clog-stack">{msg.stack}</span> : null}
      </span>
      {msg.url ? (
        <span className="clog-pos" title={msg.url}>
          {shortUrl(msg.url)}
          {msg.line ? `:${msg.line}` : ""}
        </span>
      ) : null}
    </div>
  );
}

export default function BrowserConsole({ messages, connected, onClear, onEvaluate }: Props) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [timestamps, setTimestamps] = useState(false);
  const [expr, setExpr] = useState("");
  const [history, setHistory] = useState<string[]>([]);
  const [histIdx, setHistIdx] = useState(-1);
  const [evaluating, setEvaluating] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const filtered = useMemo(() => {
    const f = FILTERS.find((x) => x.key === filter) ?? FILTERS[0];
    const q = query.trim().toLowerCase();
    return messages.filter((m) => {
      if (f.rank >= 0 && (LEVEL_RANK[m.level] ?? 1) < f.rank) return false;
      if (q) {
        const hay = (m.text + " " + (m.items ?? []).map((i) => i.v ?? "").join(" ")).toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [messages, filter, query]);

  const run = async () => {
    const value = expr.trim();
    if (!value || evaluating) return;
    setEvaluating(true);
    setHistory((h) => [value, ...h].slice(0, 50));
    setHistIdx(-1);
    try {
      await onEvaluate(value);
    } finally {
      setEvaluating(false);
      setExpr("");
    }
  };

  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      void run();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const next = Math.min(histIdx + 1, history.length - 1);
      if (history[next] !== undefined) {
        setHistIdx(next);
        setExpr(history[next]);
      }
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = histIdx - 1;
      if (next < 0) {
        setHistIdx(-1);
        setExpr("");
      } else {
        setHistIdx(next);
        setExpr(history[next]);
      }
    }
  };

  const errorCount = messages.filter((m) => m.level === "error").length;
  const warnCount = messages.filter((m) => m.level === "warning").length;

  return (
    <div className="bconsole">
      <div className="bconsole-toolbar">
        <select value={filter} onChange={(e) => setFilter(e.target.value)} title="级别筛选">
          {FILTERS.map((f) => (
            <option key={f.key} value={f.key}>
              {f.label}
            </option>
          ))}
        </select>
        <input
          className="bconsole-filter"
          type="text"
          placeholder="筛选"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          spellCheck={false}
        />
        <label className="bconsole-toggle" title="显示时间戳">
          <input
            type="checkbox"
            checked={timestamps}
            onChange={(e) => setTimestamps(e.target.checked)}
          />
          时间戳
        </label>
        <span className="bconsole-counts">
          <span className="badge error">{errorCount}</span>
          <span className="badge warning">{warnCount}</span>
        </span>
        <span className="bconsole-actions">
          <button className="ghost" onClick={onClear}>
            清空
          </button>
        </span>
      </div>
      <div className="bconsole-list" ref={listRef}>
        {!connected ? <div className="hint">浏览器控制台未连接, 正在重试...</div> : null}
        {connected && filtered.length === 0 ? <div className="hint">暂无输出。</div> : null}
        {filtered.map((m, i) => (
          <ConsoleRow key={i} msg={m} timestamps={timestamps} />
        ))}
      </div>
      <div className="bconsole-input-row">
        <span className="bconsole-prompt">&gt;</span>
        <input
          className="bconsole-input"
          value={expr}
          onChange={(e) => setExpr(e.target.value)}
          onKeyDown={onKey}
          placeholder={evaluating ? "执行中..." : "在页面中执行 JS 表达式(Enter 执行)"}
          disabled={evaluating}
          spellCheck={false}
          autoComplete="off"
        />
      </div>
    </div>
  );
}
