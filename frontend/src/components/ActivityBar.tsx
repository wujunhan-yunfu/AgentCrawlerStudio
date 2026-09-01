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

function branch() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="22"
      height="22"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="4.7" cy="12" r="1.3" />
      <circle cx="4.7" cy="4" r="1.3" />
      <circle cx="11.3" cy="4" r="1.3" />
      <path d="M4.7 5.3v5.3" />
      <path d="M6 12h4a1.3 1.3 0 0 0 1.3-1.3V7.3" />
      <path d="M9.3 9.3l2-2 2 2" />
    </svg>
  );
}

function spider() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="22"
      height="22"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3.3 2.7v1.3l3.4 3.4" />
      <path d="M1.7 6.3l1 1h4" />
      <path d="M2.7 12.7v-1.4l4-4" />
      <path d="M12.7 2.7v1.3l-3.4 3.4" />
      <path d="M14.3 6.3l-1 1h-4" />
      <path d="M13.3 12.7v-1.4l-4-4" />
      <circle cx="8" cy="10" r="2.7" />
      <circle cx="8" cy="6" r="1.35" />
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
    icon: spider(),
  },
  {
    key: "versions",
    title: "源码管理",
    icon: branch(),
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
