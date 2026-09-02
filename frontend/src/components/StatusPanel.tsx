import type { Status } from "../types";

export default function StatusPanel({ status }: { status: Status | null }) {
  const dot = (ok: boolean) => <span className={ok ? "ok" : "bad"}>●</span>;
  const m = status?.capture;

  return (
    <div className="status">
      {!status ? (
        "连接中..."
      ) : (
        <>
          <div>Xvfb: {dot(status.xvfb)}</div>
          <div>
            Chrome: {dot(status.chrome)} {status.chrome && <span className="hint">{status.chrome_cdp}</span>}
          </div>
          <div>
            抓屏: {dot(Boolean(m?.running))} {m?.fps ? ` ${m.fps} fps` : ""}
          </div>
          <div>
            已推帧: {m?.frames_total ?? "-"}
          </div>
          <div>末帧延迟: {m?.last_frame_age_ms != null ? `${m.last_frame_age_ms} ms` : "-"}</div>
          {status.error && <div className="bad">错误: {status.error}</div>}
          {m?.error && <div className="bad">推流: {m.error}</div>}
        </>
      )}
    </div>
  );
}
