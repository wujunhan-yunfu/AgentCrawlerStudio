import { useState } from "react";
import { navigate } from "../utils/api";
import { useI18n } from "../i18n";

export default function BrowserControls() {
  const { t } = useI18n();
  const [url, setUrl] = useState("");

  const goto = async (newPage: boolean) => {
    const target = url.trim() || "https://example.com";
    try {
      await navigate(target, newPage);
    } catch (e) {
      window.alert(t("browser.opFailed", { error: e instanceof Error ? e.message : String(e) }));
    }
  };

  const reload = async () => {
    try {
      await navigate("about:blank", false);
      window.location.reload();
    } catch {
      /* ignore */
    }
  };

  return (
    <div>
      <input
        type="text"
        placeholder="https://example.com"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") void goto(false);
        }}
      />
      <div className="row">
        <button className="primary" onClick={() => void goto(false)}>
          {t("browser.go")}
        </button>
        <button className="ghost" onClick={() => void goto(true)}>
          {t("browser.newTab")}
        </button>
        <button className="ghost" onClick={() => void reload()}>
          {t("browser.reload")}
        </button>
      </div>
    </div>
  );
}
