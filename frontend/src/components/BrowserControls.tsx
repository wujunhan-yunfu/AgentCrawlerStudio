import { useState } from "react";
import { navigate } from "../utils/api";

export default function BrowserControls() {
  const [url, setUrl] = useState("");

  const goto = async (newPage: boolean) => {
    const target = url.trim() || "https://example.com";
    try {
      await navigate(target, newPage);
    } catch (e) {
      window.alert(`操作失败: ${e instanceof Error ? e.message : e}`);
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
          跳转
        </button>
        <button className="ghost" onClick={() => void goto(true)}>
          新标签打开
        </button>
        <button className="ghost" onClick={() => void reload()}>
          刷新
        </button>
      </div>
    </div>
  );
}
