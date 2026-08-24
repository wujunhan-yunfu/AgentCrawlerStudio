import type { ReactNode } from "react";
import type { PanelKey } from "../types";

interface Item {
  key: PanelKey;
  title: string;
  icon: ReactNode;
}

function icon(d: string) {
  return (
    <svg viewBox="0 0 16 16" width="22" height="22" fill="currentColor" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

const ITEMS: Item[] = [
  {
    key: "code",
    title: "代码",
    icon: icon("M4.7 4.3L1.5 8l3.2 3.7.8-.7L3 8l2.5-3-.8-.7zm6.6 0l-.8.7L13 8l-2.5 3 .8.7 3.2-3.7-.8-.7-.8.7-.7-.7zM9 3.5l-2 9h1.2l2-9H9z"),
  },
  {
    key: "browser",
    title: "浏览器控制",
    icon: icon("M2 2h12v12H2V2zm1 1v5h10V3H3zm0 6v3h10V9H3z"),
  },
  {
    key: "status",
    title: "状态",
    icon: icon("M15.5 8.5H13L11 5.2 8.3 10l-2-3.4L4.5 8.5H1.5v1h4l1-1.4 1.7 3 2.8-4.8 1.5 2.7h2.5v-.5z"),
  },
  {
    key: "pages",
    title: "打开页面",
    icon: icon("M1 3h14v10H1V3zm1 1v8h12V4H2z"),
  },
  {
    key: "tools",
    title: "工具",
    icon: icon("M9.4 1.6h-2l-.3 1.6c-.4.1-.8.4-1.1.7l-1.5-.6-1 1 .6 1.5c-.3.3-.6.7-.7 1.1l-1.6.3v2l1.6.3c.1.4.4.8.7 1.1l-.6 1.5 1 1 1.5-.6c.3.3.7.6 1.1.7l.3 1.6h2l.3-1.6c.4-.1.8-.4 1.1-.7l1.5.6 1-1-.6-1.5c.3-.3.6-.7.7-1.1l1.6-.3v-2l-1.6-.3c-.1-.4-.4-.8-.7-1.1l.6-1.5-1-1-1.5.6c-.3-.3-.7-.6-1.1-.7L9.4 1.6zM8.4 10.7A2.3 2.3 0 1 1 8.4 6a2.3 2.3 0 0 1 0 4.7z"),
  },
  {
    key: "agent",
    title: "爬虫 Agent",
    icon: icon("M2 2h4v4H2V2zm4 4h4v4H6V6zm4-4h4v4h-4V2zm-4 8h4v4H6v-4zm-2 2H2v4h4v-4H4zm10-6h-1.5L12 7.5 10.5 6H9V5h1.5L12 3.5 13.5 5H15v1zm-1 3h1v4.5L13.5 13H9v-1h4v-1.5h1V9zm-8 2v1H4v1H2.5V11H1V9h1.5v2H6z"),
  },
];

interface Props {
  active: PanelKey | null;
  onSelect: (key: PanelKey) => void;
}

export default function ActivityBar({ active, onSelect }: Props) {
  return (
    <nav className="activitybar">
      {ITEMS.map((item) => (
        <button
          key={item.key}
          className={active === item.key ? "active" : ""}
          title={item.title}
          onClick={() => onSelect(item.key)}
        >
          {item.icon}
        </button>
      ))}
    </nav>
  );
}
