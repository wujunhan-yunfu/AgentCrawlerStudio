import { useState } from "react";
import { restart, screenshotBlob } from "../utils/api";

export default function ScreenshotPanel() {
  const [shotUrl, setShotUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const take = async () => {
    setLoading(true);
    setShotUrl(null);
    try {
      const blob = await screenshotBlob();
      setShotUrl(URL.createObjectURL(blob));
    } catch {
      setShotUrl(null);
    } finally {
      setLoading(false);
    }
  };

  const doRestart = async () => {
    if (!window.confirm("确认重启整个推流链路?")) return;
    try {
      await restart();
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="screenshot">
      <div className="row">
        <button className="ghost" onClick={() => void take()} disabled={loading}>
          {loading ? "截图生成中..." : "截图"}
        </button>
        <button className="danger" onClick={() => void doRestart()}>
          重启推流
        </button>
      </div>
      {shotUrl && <img src={shotUrl} alt="截图" />}
    </div>
  );
}
