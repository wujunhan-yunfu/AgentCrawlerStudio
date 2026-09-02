import { useEffect, useRef, useState } from "react";
import { api, pageClientId } from "../utils/api";

export interface LiveState {
  imgRef: React.RefObject<HTMLImageElement>;
  connected: boolean;
  lagMs: number | null;
  fps: number | null;
  width: number;
  height: number;
  /** 后端检测到已有连接, 等待用户决定取消或接管原连接 */
  conflict: boolean;
  /** 本窗口被其他窗口接管 */
  kicked: boolean;
  /** 已停止自动重连(取消连接或被接管) */
  stopped: boolean;
  resolveConflict: (decision: "cancel" | "kick") => void;
}

export function useLiveStream(): LiveState {
  const imgRef = useRef<HTMLImageElement>(null);
  const [connected, setConnected] = useState(false);
  const [lagMs, setLagMs] = useState<number | null>(null);
  const [fps, setFps] = useState<number | null>(null);
  const [width, setWidth] = useState(1280);
  const [height, setHeight] = useState(800);
  const [conflict, setConflict] = useState(false);
  const [kicked, setKicked] = useState(false);
  const [stopped, setStopped] = useState(false);

  const clockOffsetRef = useRef(0);
  const haveOffsetRef = useRef(false);
  const lastFrameArriveRef = useRef(performance.now());
  const lastFrameSeenRef = useRef(0);
  const pendingRevokeRef = useRef<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const giveUpRef = useRef(false);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retryTimer: number | undefined;
    const img = imgRef.current;

    const connect = () => {
      if (giveUpRef.current) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const q = `?client_id=${encodeURIComponent(pageClientId())}`;
      ws = new WebSocket(`${proto}://${location.host}${api(`/ws/live${q}`)}`);
      wsRef.current = ws;
      ws.binaryType = "arraybuffer";

      ws.onopen = () => setConnected(true);

      ws.onmessage = (ev) => {
        if (typeof ev.data === "string") {
          try {
            const m = JSON.parse(ev.data) as {
              type?: string;
              server_time?: number;
              width?: number;
              height?: number;
            };
            if (m.type === "conflict") {
              setConnected(false);
              setConflict(true);
              return;
            }
            if (m.type === "kicked") {
              setKicked(true);
              setStopped(true);
              giveUpRef.current = true;
              setConnected(false);
              try {
                ws?.close();
              } catch {
                /* ignore */
              }
              return;
            }
            if (m.type === "hello") {
              clockOffsetRef.current = (m.server_time ?? 0) - Date.now() / 1000;
              haveOffsetRef.current = true;
              if (m.width) setWidth(m.width);
              if (m.height) setHeight(m.height);
            }
          } catch {
            /* ignore */
          }
          return;
        }
        const dv = new DataView(ev.data as ArrayBuffer);
        const ts = dv.getFloat64(0, true);
        const blob = new Blob([(ev.data as ArrayBuffer).slice(8)], { type: "image/jpeg" });
        const url = URL.createObjectURL(blob);
        if (pendingRevokeRef.current) URL.revokeObjectURL(pendingRevokeRef.current);
        pendingRevokeRef.current = url;
        if (img) {
          img.onload = () => {
            if (pendingRevokeRef.current) {
              URL.revokeObjectURL(pendingRevokeRef.current);
              pendingRevokeRef.current = null;
            }
            if (haveOffsetRef.current) {
              const now = Date.now() / 1000;
              setLagMs(Math.max(0, now - clockOffsetRef.current - ts) * 1000);
            }
            const cur = performance.now();
            const dt = cur - lastFrameArriveRef.current;
            lastFrameArriveRef.current = cur;
            if (dt > 0 && dt < 1000) {
              lastFrameSeenRef.current = lastFrameSeenRef.current * 0.9 + (1000 / dt) * 0.1;
              setFps(lastFrameSeenRef.current);
            }
          };
          img.src = url;
        }
      };

      ws.onclose = () => {
        setConnected(false);
        if (!closed && !giveUpRef.current) {
          retryTimer = window.setTimeout(connect, 1000);
        }
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          /* ignore */
        }
      };
    };

    connect();
    return () => {
      closed = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    };
  }, []);

  const resolveConflict = (decision: "cancel" | "kick") => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    setConflict(false);
    if (decision === "cancel") {
      giveUpRef.current = true;
      setStopped(true);
      try {
        ws.send(JSON.stringify({ type: "cancel" }));
      } catch {
        /* ignore */
      }
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    } else {
      try {
        ws.send(JSON.stringify({ type: "kick" }));
      } catch {
        /* ignore */
      }
      setConnected(true);
    }
  };

  return { imgRef, connected, lagMs, fps, width, height, conflict, kicked, stopped, resolveConflict };
}
