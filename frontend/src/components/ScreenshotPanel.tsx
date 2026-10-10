import { useState } from "react";
import { restart, screenshotBlob } from "../utils/api";
import { useI18n } from "../i18n";

export default function ScreenshotPanel() {
  const { t } = useI18n();
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
    if (!window.confirm(t("tools.confirmRestart"))) return;
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
          {loading ? t("tools.capturing") : t("tools.capture")}
        </button>
        <button className="danger" onClick={() => void doRestart()}>
          {t("tools.restart")}
        </button>
      </div>
      {shotUrl && <img src={shotUrl} alt={t("tools.screenshot")} />}
    </div>
  );
}
