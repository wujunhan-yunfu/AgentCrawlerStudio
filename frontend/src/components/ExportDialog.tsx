import { useState } from "react";
import { exportScript, validateCron } from "../utils/api";

interface Props {
  code: string;
  onClose: () => void;
  onError: (message: string) => void;
}

function fmtTime(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function ExportDialog({ code, onClose, onError }: Props) {
  const [name, setName] = useState("crawler");
  const [cron, setCron] = useState("");
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [exportError, setExportError] = useState("");
  const [checkResult, setCheckResult] = useState<{ ok: boolean; error: string; next: number[] } | null>(
    null,
  );

  const doValidate = async () => {
    const expr = cron.trim();
    if (!expr) {
      setCheckResult({ ok: false, error: "请先输入 cron 表达式", next: [] });
      return;
    }
    setChecking(true);
    try {
      const r = await validateCron(expr);
      setCheckResult({ ok: r.ok, error: r.error, next: r.next_runs ?? [] });
    } catch (e) {
      setCheckResult({ ok: false, error: e instanceof Error ? e.message : String(e), next: [] });
    } finally {
      setChecking(false);
    }
  };

  const doExport = async () => {
    if (busy) return;
    if (cron.trim()) {
      const r = await validateCron(cron.trim()).catch((e) => ({
        ok: false,
        error: e instanceof Error ? e.message : String(e),
        next_runs: [],
      }));
      setCheckResult({ ok: r.ok, error: r.error, next: r.next_runs ?? [] });
      if (!r.ok) return;
    }
    setBusy(true);
    setExportError("");
    try {
      const blob = await exportScript({ code, name: name.trim(), cron: cron.trim() });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${name.trim() || "crawler"}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      onClose();
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setExportError(message);
      onError(message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="export-backdrop" onClick={onClose}>
      <div className="export-card" onClick={(e) => e.stopPropagation()}>
        <div className="export-title">导出脚本</div>
        <div className="export-desc">
          将当前编辑器中的代码导出为可直接用 uv 运行的独立包, 内含 pyproject.toml、README 与运行入口。
        </div>

        <div className="export-field">
          <div className="export-label">包名</div>
          <input
            className="export-input"
            value={name}
            maxLength={64}
            placeholder="crawler"
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        <div className="export-field">
          <div className="export-label">定时运行 cron(可选)</div>
          <div className="export-cron-row">
            <input
              className="export-input"
              value={cron}
              placeholder="留空则不设默认, 定时运行须用 --cron 提供, 如 */5 * * * *"
              onChange={(e) => {
                setCron(e.target.value);
                setCheckResult(null);
              }}
            />
            <button onClick={() => void doValidate()} disabled={checking || busy}>
              {checking ? "校验中..." : "校验"}
            </button>
          </div>
          <div className="export-hint">分 时 日 月 周; 支持 * , - / 与 JAN-DEC / SUN-SAT 及 @daily 等宏</div>
          {checkResult ? (
            checkResult.ok ? (
              <div className="export-ok">
                表达式有效
                {checkResult.next.length ? `, 接下来运行: ${checkResult.next.map(fmtTime).join(", ")}` : ""}
              </div>
            ) : (
              <div className="export-err">表达式无效: {checkResult.error}</div>
            )
          ) : null}
        </div>

        {exportError ? <div className="export-err">导出失败: {exportError}</div> : null}

        <div className="export-actions">
          <button className="primary" disabled={busy || !code.trim()} onClick={() => void doExport()}>
            {busy ? "导出中..." : "导出 ZIP"}
          </button>
          <button onClick={onClose} disabled={busy}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
