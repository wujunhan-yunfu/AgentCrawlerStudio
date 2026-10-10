import { useState } from "react";
import { exportScript, validateCron } from "../utils/api";
import { useI18n } from "../i18n";

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
  const { t } = useI18n();
  const [name, setName] = useState("crawler");
  const [cron, setCron] = useState("");
  const [headless, setHeadless] = useState(false);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [exportError, setExportError] = useState("");
  const [checkResult, setCheckResult] = useState<{ ok: boolean; error: string; next: number[] } | null>(
    null,
  );

  const doValidate = async () => {
    const expr = cron.trim();
    if (!expr) {
      setCheckResult({ ok: false, error: t("export.needCron"), next: [] });
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
      const blob = await exportScript({ code, name: name.trim(), cron: cron.trim(), headless });
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
        <div className="export-title">{t("export.title")}</div>
        <div className="export-desc">
          {t("export.desc")}
        </div>

        <div className="export-field">
          <div className="export-label">{t("export.packageName")}</div>
          <input
            className="export-input"
            value={name}
            maxLength={64}
            placeholder="crawler"
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        <div className="export-field">
          <div className="export-label">{t("export.cronLabel")}</div>
          <div className="export-cron-row">
            <input
              className="export-input"
              value={cron}
              placeholder={t("export.cronPlaceholder")}
              onChange={(e) => {
                setCron(e.target.value);
                setCheckResult(null);
              }}
            />
            <button onClick={() => void doValidate()} disabled={checking || busy}>
              {checking ? t("export.checking") : t("export.validate")}
            </button>
          </div>
          <div className="export-hint">{t("export.cronHint")}</div>
          {checkResult ? (
            checkResult.ok ? (
              <div className="export-ok">
                {t("export.valid")}
                {checkResult.next.length ? t("export.nextRuns", { runs: checkResult.next.map(fmtTime).join(", ") }) : ""}
              </div>
            ) : (
              <div className="export-err">{t("export.invalid", { error: checkResult.error })}</div>
            )
          ) : null}
        </div>

        <div className="export-field">
          <label className="export-check">
            <input
              type="checkbox"
              checked={headless}
              onChange={(e) => setHeadless(e.target.checked)}
            />
            <span>{t("export.headless")}</span>
          </label>
          <div className="export-hint">
            {t("export.headlessHint")}
          </div>
        </div>

        {exportError ? <div className="export-err">{t("export.failed", { error: exportError })}</div> : null}

        <div className="export-actions">
          <button className="primary" disabled={busy || !code.trim()} onClick={() => void doExport()}>
            {busy ? t("export.exporting") : t("export.exportZip")}
          </button>
          <button onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </button>
        </div>
      </div>
    </div>
  );
}
