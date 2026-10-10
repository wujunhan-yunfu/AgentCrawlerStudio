import type { ReactNode } from "react";
import type { PanelKey, Status } from "../types";
import type { VersionsState } from "../hooks/useVersions";
import { useI18n } from "../i18n";
import type { MessageKey } from "../i18n";
import AgentPanel from "./AgentPanel";
import BrowserControls from "./BrowserControls";
import PagesList from "./PagesList";
import ScreenshotPanel from "./ScreenshotPanel";
import StatusPanel from "./StatusPanel";
import VersionsPanel from "./VersionsPanel";

interface Props {
  panel: PanelKey | null;
  onClose: () => void;
  status: Status | null;
  onAgentCode?: (code: string, base?: string | null) => void;
  versions?: VersionsState;
  width: number;
  onResizeStart: (e: React.MouseEvent<HTMLDivElement>) => void;
}

const TITLES: Record<PanelKey, MessageKey> = {
  code: "activity.code",
  browser: "activity.browser",
  status: "activity.status",
  pages: "activity.pages",
  tools: "activity.tools",
  agent: "activity.agent",
  versions: "activity.versions",
};

export default function Sidebar({ panel, onClose, status, onAgentCode, versions, width, onResizeStart }: Props) {
  const { t } = useI18n();
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
    case "versions":
      body = versions ? <VersionsPanel versions={versions} /> : null;
      break;
  }

  return (
    <aside
      className={`sidebar${panel ? "" : " hidden"}${panel === "agent" ? " agent" : ""}`}
      style={{ width: panel ? width : 0 }}
    >
      {panel !== "agent" ? (
        <div className="sidebar-title">
          <span>{panel ? t(TITLES[panel]) : ""}</span>
          {panel && (
            <button className="close" title={t("sidebar.close")} onClick={onClose}>
              <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true">
                <path d="M12 4l-4 4 4 4-1 1-4-4-4 4-1-1 4-4-4-4 1-1 4 4 4-4 1 1z" />
              </svg>
            </button>
          )}
        </div>
      ) : null}
      <div className={`sidebar-body${panel === "agent" ? " agent" : ""}`}>{body}</div>
      <div className="sidebar-resize-handle" title={t("sidebar.resize")} onMouseDown={onResizeStart} />
    </aside>
  );
}
