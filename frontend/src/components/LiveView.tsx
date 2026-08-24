import { useRef } from "react";
import type { HighlightBox } from "./ElementsPanel";

interface Props {
  imgRef: React.RefObject<HTMLImageElement>;
  connected: boolean;
  lagMs: number | null;
  fps: number | null;
  hasBrowser: boolean;
  connecting: boolean;
  width: number;
  height: number;
  highlight: HighlightBox | null;
  maximized: boolean;
  onToggle: () => void;
}

function BrowserLogo({ title }: { title: string }) {
  return (
    <div className="logo">
      <svg viewBox="0 0 64 64" width="96" height="96" aria-hidden="true">
        <defs>
          <linearGradient id="logo-grad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#2b6cb0" />
            <stop offset="100%" stopColor="#38a169" />
          </linearGradient>
        </defs>
        <circle cx="32" cy="32" r="30" fill="url(#logo-grad)" opacity="0.28" />
        <rect x="15" y="19" width="34" height="27" rx="3.5" fill="none" stroke="#9aa0a6" strokeWidth="3" />
        <path d="M15 26.5h34" stroke="#9aa0a6" strokeWidth="3" />
        <circle cx="20.5" cy="22.5" r="1.5" fill="#f56565" />
        <circle cx="26.5" cy="22.5" r="1.5" fill="#ecc94b" />
        <circle cx="32.5" cy="22.5" r="1.5" fill="#48bb78" />
        <path d="M20 34h9M20 39.5h9" stroke="#48bb78" strokeWidth="2.5" strokeLinecap="round" />
      </svg>
      <span>{title}</span>
    </div>
  );
}

export default function LiveView({ imgRef, connected, lagMs, fps, hasBrowser, connecting, width, height, highlight, maximized, onToggle }: Props) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const toggle = onToggle;
  const logoTitle = connecting ? "正在连接服务器..." : "无活跃浏览器进程";

  const highlightStyle = ((): React.CSSProperties | null => {
    const img = imgRef.current;
    const canvas = canvasRef.current;
    if (!img || !canvas || !highlight || !width || !height) return null;
    const imgRect = img.getBoundingClientRect();
    const canvasRect = canvas.getBoundingClientRect();
    const naturalRatio = width / height;
    const elementRatio = imgRect.width / imgRect.height;
    let contentW = imgRect.width;
    let contentH = imgRect.height;
    if (elementRatio > naturalRatio) {
      contentH = imgRect.height;
      contentW = contentH * naturalRatio;
    } else {
      contentW = imgRect.width;
      contentH = contentW / naturalRatio;
    }
    const scaleX = contentW / width;
    const scaleY = contentH / height;
    return {
      left: imgRect.left - canvasRect.left + (imgRect.width - contentW) / 2 + highlight.x * scaleX,
      top: imgRect.top - canvasRect.top + (imgRect.height - contentH) / 2 + highlight.y * scaleY,
      width: Math.max(1, highlight.w * scaleX),
      height: Math.max(1, highlight.h * scaleY),
    };
  })();

  return (
    <>
      {maximized && <div className="backdrop" onClick={toggle} />}
      <div
        className={`live-window${maximized ? " maximized" : ""}`}
        onClick={toggle}
        title={maximized ? "点击缩小" : "点击放大"}
      >
        <div className="canvas" ref={canvasRef}>
          <img ref={imgRef} className="live" alt="实时画面" draggable={false} />
          {(!hasBrowser || connecting) && <BrowserLogo title={logoTitle} />}
          {highlightStyle ? <div className="live-highlight" style={highlightStyle} /> : null}
          <div className="live-stats">
            <span className={connected ? "ok" : "bad"}>{connected ? "实时画面" : "重连中..."}</span>
            <span>延迟 {lagMs != null ? `${Math.round(lagMs)} ms` : "-"}</span>
            <span>{fps != null ? `${Math.round(fps)} fps` : "-"}</span>
          </div>
        </div>
      </div>
    </>
  );
}
