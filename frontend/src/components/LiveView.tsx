import { useRef, useState } from "react";
import type { RemoteControlState } from "../hooks/useRemoteControl";
import type { HighlightBox } from "./ElementsPanel";

interface Props {
  imgRef: React.RefObject<HTMLImageElement>;
  connected: boolean;
  lagMs: number | null;
  fps: number | null;
  hasBrowser: boolean;
  connecting: boolean;
  /** 已停止自动重连(取消连接或被接管) */
  stopped: boolean;
  /** 被其他窗口接管 */
  kicked: boolean;
  width: number;
  height: number;
  highlight: HighlightBox | null;
  maximized: boolean;
  onToggle: () => void;
  control: RemoteControlState;
  running: boolean;
}

interface Mods {
  alt: boolean;
  ctrl: boolean;
  meta: boolean;
  shift: boolean;
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

function mods(e: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }): Mods {
  return { alt: e.altKey, ctrl: e.ctrlKey, meta: e.metaKey, shift: e.shiftKey };
}

export default function LiveView({
  imgRef, connected, lagMs, fps, hasBrowser, connecting, stopped, kicked, width, height,
  highlight, maximized, onToggle, control, running,
}: Props) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const controlRef = useRef<HTMLDivElement>(null);
  const pendingMoveRef = useRef<{ x: number; y: number; buttons: number; modifiers: Mods } | null>(null);
  const rafRef = useRef(0);
  const feedbackTimerRef = useRef<number | null>(null);
  const [controlOn, setControlOn] = useState(true);
  const [textDraft, setTextDraft] = useState("");
  const [textFeedback, setTextFeedback] = useState<{ text: string; error: boolean } | null>(null);

  const logoTitle = connecting ? "正在连接服务器..." : "无活跃浏览器进程";
  // 远程控制仅在放大窗口时可用; 缩小(右侧)时保持原行为: 点击窗口放大
  const controlActive = maximized && controlOn && !running && control.enabled && control.connected && hasBrowser;

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

  // 容器(client)坐标 → 图像像素坐标(与高亮换算同源)
  const toImageCoords = (clientX: number, clientY: number): { x: number; y: number } | null => {
    const canvas = canvasRef.current;
    if (!canvas || !width || !height) return null;
    const rect = canvas.getBoundingClientRect();
    const naturalRatio = width / height;
    const elementRatio = rect.width / rect.height;
    let contentW = rect.width;
    let contentH = rect.height;
    if (elementRatio > naturalRatio) {
      contentH = rect.height;
      contentW = contentH * naturalRatio;
    } else {
      contentW = rect.width;
      contentH = contentW / naturalRatio;
    }
    const imgX = (clientX - rect.left - (rect.width - contentW) / 2) * (width / contentW);
    const imgY = (clientY - rect.top - (rect.height - contentH) / 2) * (height / contentH);
    return { x: Math.round(imgX), y: Math.round(imgY) };
  };

  const flushMove = () => {
    rafRef.current = 0;
    const p = pendingMoveRef.current;
    if (p) {
      pendingMoveRef.current = null;
      control.send({ type: "mouse", action: "move", ...p });
    }
  };

  const queueMove = (x: number, y: number, buttons: number, m: Mods) => {
    pendingMoveRef.current = { x, y, buttons, modifiers: m };
    if (rafRef.current) return;
    rafRef.current = requestAnimationFrame(flushMove);
  };

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!controlActive) return;
    const c = toImageCoords(e.clientX, e.clientY);
    if (!c) return;
    e.preventDefault();
    controlRef.current?.focus();
    controlRef.current?.setPointerCapture(e.pointerId);
    control.send({
      type: "mouse", action: "down",
      x: c.x, y: c.y, button: e.button, buttons: e.buttons,
      clickCount: e.detail || 1, modifiers: mods(e),
    });
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!controlActive) return;
    const c = toImageCoords(e.clientX, e.clientY);
    if (!c) return;
    queueMove(c.x, c.y, e.buttons, mods(e));
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!controlActive) return;
    const c = toImageCoords(e.clientX, e.clientY);
    if (!c) return;
    control.send({
      type: "mouse", action: "up",
      x: c.x, y: c.y, button: e.button, buttons: e.buttons,
      clickCount: e.detail || 1, modifiers: mods(e),
    });
    try {
      controlRef.current?.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  };

  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (!controlActive) return;
    const c = toImageCoords(e.clientX, e.clientY);
    if (!c) return;
    e.preventDefault();
    e.stopPropagation();
    control.send({
      type: "mouse", action: "wheel",
      x: c.x, y: c.y, deltaX: e.deltaX, deltaY: e.deltaY, modifiers: mods(e),
    });
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!controlActive) return;
    if (e.nativeEvent.isComposing) return;  // IME 组合期间跳过逐键, 由 compositionend 处理
    e.preventDefault();
    e.stopPropagation();
    const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    control.send({
      type: "key", action: "down",
      key: e.key, code: e.code, keyCode: e.keyCode,
      text: printable ? e.key : undefined,
      modifiers: mods(e),
    });
  };

  const onKeyUp = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!controlActive) return;
    if (e.nativeEvent.isComposing) return;
    e.preventDefault();
    e.stopPropagation();
    control.send({
      type: "key", action: "up",
      key: e.key, code: e.code, keyCode: e.keyCode, modifiers: mods(e),
    });
  };

  // 直接在实时画面内键入中文: 组合结束时把整段提交文本整段插入页面
  const onCompositionStart = () => {
    if (!controlActive) return;
    if (controlRef.current) controlRef.current.textContent = "";
  };

  const onCompositionEnd = (e: React.CompositionEvent<HTMLDivElement>) => {
    if (!controlActive) return;
    e.preventDefault();
    if (e.data) control.send({ type: "text", action: "insert", text: e.data });
    if (controlRef.current) controlRef.current.textContent = "";
  };

  // 文本输入框: 输入框内中文 IME 组合实时预览到页面(compose), 组合结束时提交(commit)
  const submitText = () => {
    const t = textDraft;
    if (!t.trim()) {
      setTextDraft("");
      return;
    }
    control.send({ type: "text", action: "insert", text: t });
    setTextDraft("");
    setTextFeedback({ text: "已发送", error: false });
    if (feedbackTimerRef.current) window.clearTimeout(feedbackTimerRef.current);
    feedbackTimerRef.current = window.setTimeout(() => setTextFeedback(null), 1500);
  };

  const onTextKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submitText();
    }
  };

  const onTextCompositionUpdate = (e: React.CompositionEvent<HTMLTextAreaElement>) => {
    if (!controlActive || !e.data) return;
    control.send({
      type: "text", action: "compose",
      text: e.data, selectionStart: 0, selectionEnd: e.data.length,
    });
  };

  const onTextCompositionEnd = (e: React.CompositionEvent<HTMLTextAreaElement>) => {
    if (!controlActive || !e.data) return;
    control.send({ type: "text", action: "commit", text: e.data });
    setTextDraft("");
  };

  const textFeedbackText = control.error ?? textFeedback?.text;

  const hint = (() => {
    if (!controlOn) return "远程控制已关闭";
    if (running) return "脚本执行中，远程控制已暂停";
    if (!control.connected) return "控制通道连接中...";
    if (!control.enabled) return "浏览器无活动页面";
    return null;
  })();

  return (
    <>
      {maximized && <div className="backdrop" onClick={onToggle} />}
      <div
        className={`live-window${maximized ? " maximized" : ""}`}
        onClick={() => {
          if (!maximized) onToggle();
          else if (!controlActive) onToggle();
        }}
        title={maximized ? undefined : "点击放大"}
      >
        <div className="canvas" ref={canvasRef}>
          <img ref={imgRef} className="live" alt="实时画面" draggable={false} />
          {(!hasBrowser || connecting) && <BrowserLogo title={logoTitle} />}
          {highlightStyle ? <div className="live-highlight" style={highlightStyle} /> : null}
          <div
            ref={controlRef}
            className={`live-control${controlActive ? " active" : ""}`}
            tabIndex={0}
            contentEditable={controlActive ? "plaintext-only" : false}
            suppressContentEditableWarning
            spellCheck={false}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onWheel={onWheel}
            onKeyDown={onKeyDown}
            onKeyUp={onKeyUp}
            onCompositionStart={onCompositionStart}
            onCompositionEnd={onCompositionEnd}
          />
          {maximized ? (
            <div className="live-toolbar">
              <button
                title={controlOn ? "关闭远程控制" : "开启远程控制"}
                onClick={(e) => {
                  e.stopPropagation();
                  setControlOn((v) => !v);
                }}
              >
                {controlOn ? "🖱" : "🚫"}
              </button>
              <button title="点击缩小" onClick={(e) => { e.stopPropagation(); onToggle(); }}>
                ⤡
              </button>
            </div>
          ) : null}
          {maximized && hint ? <div className="live-control-hint">{hint}</div> : null}
          {maximized && controlOn && !running ? (
            <div className="live-textinput">
              <textarea
                rows={1}
                placeholder="输入文本，Enter 发送（支持中文 IME）"
                value={textDraft}
                onChange={(e) => setTextDraft(e.target.value)}
                onKeyDown={onTextKeyDown}
                onCompositionUpdate={onTextCompositionUpdate}
                onCompositionEnd={onTextCompositionEnd}
              />
              <button className="primary small" onClick={submitText}>
                输入
              </button>
            </div>
          ) : null}
          {textFeedbackText ? (
            <div className={`live-text-feedback${textFeedback?.error || control.error ? " error" : ""}`}>
              {textFeedbackText}
            </div>
          ) : null}
          <div className="live-stats">
            <span className={connected ? "ok" : stopped ? "bad" : undefined}>{connected ? "实时画面" : kicked ? "已被其他窗口接管" : stopped ? "未连接" : "重连中..."}</span>
            <span>延迟 {lagMs != null ? `${Math.round(lagMs)} ms` : "-"}</span>
            <span>{fps != null ? `${Math.round(fps)} fps` : "-"}</span>
            {controlActive ? <span className="ok">可操控</span> : null}
          </div>
        </div>
      </div>
    </>
  );
}
