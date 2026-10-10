import type { Status } from "../types";
import { useI18n } from "../i18n";

export default function StatusPanel({ status }: { status: Status | null }) {
  const { t } = useI18n();
  const dot = (ok: boolean) => <span className={ok ? "ok" : "bad"}>●</span>;
  const m = status?.capture;

  return (
    <div className="status">
      {!status ? (
        t("status.connecting")
      ) : (
        <>
          <div>Xvfb: {dot(status.xvfb)}</div>
          <div>
            Chrome: {dot(status.chrome)} {status.chrome && <span className="hint">{status.chrome_cdp}</span>}
          </div>
          <div>
            {t("status.capture")} {dot(Boolean(m?.running))} {m?.fps ? ` ${m.fps} fps` : ""}
          </div>
          <div>
            {t("status.frames")} {m?.frames_total ?? "-"}
          </div>
          <div>{t("status.lastFrame")} {m?.last_frame_age_ms != null ? `${m.last_frame_age_ms} ms` : "-"}</div>
          {status.error && <div className="bad">{t("status.error", { error: status.error })}</div>}
          {m?.error && <div className="bad">{t("status.stream", { error: m.error })}</div>}
        </>
      )}
    </div>
  );
}
