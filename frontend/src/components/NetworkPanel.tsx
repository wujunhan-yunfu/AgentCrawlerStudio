import { useMemo, useRef, useState } from "react";
import { useNetwork } from "../hooks/useNetwork";
import { netApi, type NetRecord } from "../utils/devtoolsApi";
import { useI18n } from "../i18n";

function urlName(url: string): string {
  try {
    const u = new URL(url);
    return (u.pathname.split("/").pop() || u.host) + u.search;
  } catch {
    return url;
  }
}

function fmtSize(n: number | null): string {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function typeOf(mimeType: string | null, type: string): string {
  if (mimeType?.includes("javascript")) return "JS";
  if (mimeType?.includes("css")) return "CSS";
  if (mimeType?.includes("html")) return "Doc";
  if (mimeType?.includes("json")) return "XHR";
  if (mimeType?.startsWith("image/")) return "Img";
  if (mimeType?.includes("font")) return "Font";
  return type || "Other";
}

function DetailView({ record, onClose }: { record: NetRecord; onClose: () => void }) {
  const { t } = useI18n();
  const [body, setBody] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [bodyErr, setBodyErr] = useState<string | null>(null);

  const loadBody = async () => {
    if (body !== null || loading) return;
    setLoading(true);
    setBodyErr(null);
    try {
      const r = await netApi.body(record.id);
      if (r.ok) setBody(r.body ?? t("network.empty"));
      else setBodyErr(r.error ?? t("network.cannotFetch"));
    } catch (e) {
      setBodyErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="net-detail">
      <div className="net-detail-head">
        <span className="net-detail-title">{t("network.detail")}</span>
        <button className="ghost" onClick={onClose}>{t("network.close")}</button>
      </div>
      <div className="net-detail-body">
        <div className="net-group">
          <h4>{t("network.general")}</h4>
          <div className="net-kv"><span className="k">{t("network.reqUrl")}</span><span className="v">{record.url}</span></div>
          <div className="net-kv"><span className="k">{t("network.reqMethod")}</span><span className="v">{record.method}</span></div>
          <div className="net-kv"><span className="k">{t("network.statusCode")}</span>
            <span className={`v ${record.status !== null && record.status >= 400 ? "bad" : "ok"}`}>
              {record.status ?? "—"} {record.statusText ?? ""}
            </span>
          </div>
          {record.error ? <div className="net-kv"><span className="k">{t("network.error")}</span><span className="v bad">{record.error}</span></div> : null}
          <div className="net-kv"><span className="k">{t("network.type")}</span><span className="v">{record.type}</span></div>
          <div className="net-kv"><span className="k">{t("network.time")}</span><span className="v">{fmtTime(record.started)}</span></div>
          <div className="net-kv"><span className="k">{t("network.duration")}</span><span className="v">{record.duration !== null ? `${record.duration} ms` : "—"}</span></div>
          <div className="net-kv"><span className="k">{t("network.size")}</span><span className="v">{fmtSize(record.size)}</span></div>
          {record.initiator ? <div className="net-kv"><span className="k">{t("network.initiator")}</span><span className="v">{record.initiator}</span></div> : null}
        </div>
        {record.postData ? (
          <div className="net-group">
            <h4>{t("network.payload")}</h4>
            <pre className="net-pre">{record.postData}</pre>
          </div>
        ) : null}
        <div className="net-group">
          <h4>{t("network.reqHeaders")}</h4>
          <pre className="net-pre">{Object.entries(record.requestHeaders ?? {}).map(([k, v]) => `${k}: ${v}`).join("\n")}</pre>
        </div>
        {record.responseHeaders ? (
          <div className="net-group">
            <h4>{t("network.resHeaders")}</h4>
            <pre className="net-pre">{Object.entries(record.responseHeaders).map(([k, v]) => `${k}: ${v}`).join("\n")}</pre>
          </div>
        ) : null}
        <div className="net-group">
          <h4>
            {t("network.resBody")}
            {body === null ? (
              <button className="ghost net-body-btn" onClick={() => void loadBody()} disabled={loading}>
                {loading ? t("network.fetching") : t("network.view")}
              </button>
            ) : null}
          </h4>
          {bodyErr ? <div className="bad">{bodyErr}</div> : null}
          {body !== null ? <pre className="net-pre">{body}</pre> : null}
        </div>
      </div>
    </div>
  );
}

export default function NetworkPanel() {
  const { t } = useI18n();
  const [preserveLog, setPreserveLog] = useState(false);
  const preserveRef = useRef(preserveLog);
  preserveRef.current = preserveLog;
  const { records, connected, clear } = useNetwork({ preserveLogRef: preserveRef });
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<NetRecord | null>(null);

  const types = useMemo(() => {
    const s = new Set<string>();
    for (const r of records) s.add(typeOf(r.mimeType, r.type));
    return ["all", ...Array.from(s)];
  }, [records]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return records
      .filter((r) => {
        if (filter !== "all" && typeOf(r.mimeType, r.type) !== filter) return false;
        if (q && !(r.url + " " + r.method).toLowerCase().includes(q)) return false;
        return true;
      })
      .slice()
      .reverse();
  }, [records, filter, query]);

  const maxDuration = useMemo(
    () => Math.max(...records.map((r) => r.duration ?? 0), 1),
    [records]
  );

  return (
    <div className="net-panel">
      <div className="net-toolbar">
        <select value={filter} onChange={(e) => setFilter(e.target.value)} title={t("network.typeFilter")}>
          {types.map((type) => (
            <option key={type} value={type}>{type === "all" ? t("network.all") : type}</option>
          ))}
        </select>
        <input
          className="net-filter"
          type="text"
          placeholder={t("network.filterUrl")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          spellCheck={false}
        />
        <label className="net-preserve" title={t("network.preserveTitle")}>
          <input type="checkbox" checked={preserveLog} onChange={(e) => setPreserveLog(e.target.checked)} />
          {t("network.preserve")}
        </label>
        <span className="net-count">{t("network.requestCount", { count: filtered.length })}</span>
        <span className="net-actions">
          <button className="ghost" onClick={() => void clear()}>{t("network.clear")}</button>
        </span>
      </div>
      {!connected ? <div className="hint net-hint">{t("network.disconnected")}</div> : null}
      <div className="net-content">
        <div className="net-table-wrap">
          <table className="net-table">
            <thead>
              <tr>
                <th>{t("network.colName")}</th>
                <th>{t("network.colMethod")}</th>
                <th>{t("network.colStatus")}</th>
                <th>{t("network.colType")}</th>
                <th>{t("network.colSize")}</th>
                <th>{t("network.colDuration")}</th>
                <th>{t("network.colTimeline")}</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => (
                <tr
                  key={r.id}
                  className={selected?.id === r.id ? "selected" : ""}
                  onClick={() => setSelected(r)}
                >
                  <td className="net-name" title={r.url}>{urlName(r.url)}</td>
                  <td className={`net-method m-${r.method.toLowerCase()}`}>{r.method}</td>
                  <td className={`net-status ${r.status !== null && r.status >= 400 ? "bad" : r.status !== null ? "ok" : ""}`}>
                    {r.status ?? (r.error ? t("network.failed") : "…")}
                  </td>
                  <td>{typeOf(r.mimeType, r.type)}</td>
                  <td>{fmtSize(r.size)}</td>
                  <td>{r.duration !== null ? r.duration : "…"}</td>
                  <td>
                    <div className="net-timeline">
                      <div
                        className={`net-timeline-bar ${r.error ? "err" : ""}`}
                        style={{ width: `${Math.max(2, ((r.duration ?? 0) / maxDuration) * 100)}%` }}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {filtered.length === 0 && connected ? <div className="hint net-hint">{t("network.noRequests")}</div> : null}
        </div>
        {selected ? <DetailView record={selected} onClose={() => setSelected(null)} /> : null}
      </div>
    </div>
  );
}
