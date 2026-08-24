import type { ReactNode } from "react";
import type { PanelKey, Status } from "../types";
import AgentPanel from "./AgentPanel";
import BrowserControls from "./BrowserControls";
import PagesList from "./PagesList";
import ScreenshotPanel from "./ScreenshotPanel";
import StatusPanel from "./StatusPanel";

interface Props {
  panel: PanelKey | null;
  onClose: () => void;
  status: Status | null;
  onAgentCode?: (code: string, base?: string | null) => void;
  width: number;
  onResizeStart: (e: React.MouseEvent<HTMLDivElement>) => void;
}

const TITLES: Record<PanelKey, string> = {
  code: "代码",
  browser: "浏览器控制",
  status: "状态",
  pages: "打开页面",
  tools: "工具",
  agent: "爬虫 Agent",
};

export default function Sidebar({ panel, onClose, status, onAgentCode, width, onResizeStart }: Props) {
  let body: ReactNode = null;
  switch (panel) {
    case "browser":
      body = <BrowserControls />;
      break;
    case "status":
      body = <StatusPanel status={status} />;
      break;
    case "pages":
      body = <PagesList pages={status?.pages} />;
      break;
    case "tools":
      body = <ScreenshotPanel />;
      break;
    case "agent":
      body = (
        <AgentPanel
          onAgentCode={onAgentCode}
          onClose={onClose}
        />
      );
      break;
  }

  return (
    <aside
      className={`sidebar${panel ? "" : " hidden"}${panel === "agent" ? " agent" : ""}`}
      style={{ width: panel ? width : 0 }}
    >
      {panel !== "agent" ? (
        <div className="sidebar-title">
          <span>{panel ? TITLES[panel] : ""}</span>
          {panel && (
            <button className="close" title="关闭面板" onClick={onClose}>
              <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true">
                <path d="M12 4l-4 4 4 4-1 1-4-4-4 4-1-1 4-4-4-4 1-1 4 4 4-4 1 1z" />
              </svg>
            </button>
          )}
        </div>
      ) : null}
      <div className={`sidebar-body${panel === "agent" ? " agent" : ""}`}>{body}</div>
      <div className="sidebar-resize-handle" title="拖拽调整宽度" onMouseDown={onResizeStart} />
    </aside>
  );
}
