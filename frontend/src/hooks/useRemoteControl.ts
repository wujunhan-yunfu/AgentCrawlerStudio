import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../utils/api";

export interface RemoteControlState {
  send: (msg: Record<string, unknown>) => void;
  connected: boolean;
  enabled: boolean;
  viewport: { width: number; height: number };
  offset: { x: number; y: number };
  error: string | null;
}

/**
 * 远程控制输入通道: 维护 /ws/input 双向 WebSocket。
 * `send` 仅在连接就绪且后端 enabled 时下发(断线期间丢弃, 由覆盖层自行限频)。
 */
export function useRemoteControl(): RemoteControlState {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [viewport, setViewport] = useState({ width: 1280, height: 800 });
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [error, setError] = useState<string | null>(null);

  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const openRef = useRef(false);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retryTimer: number | undefined;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}${api("/ws/input")}`);
      wsRef.current = ws;

      ws.onopen = () => {
        openRef.current = true;
        setConnected(true);
      };

      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data as string) as {
            type?: string;
            viewport?: { width: number; height: number };
            offset?: { x: number; y: number };
            enabled?: boolean;
          };
          if (m.type === "hello") {
            setEnabled(m.enabled ?? false);
            setError(null);
            if (m.viewport) setViewport(m.viewport);
            if (m.offset) setOffset(m.offset);
          } else if (m.type === "disabled") {
            setEnabled(false);
            setError(null);
          } else if (m.type === "error") {
            setError((m as { message?: string }).message ?? "操作失败");
          }
        } catch {
          /* ignore */
        }
      };

      ws.onclose = () => {
        openRef.current = false;
        setConnected(false);
        if (!closed) {
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
      openRef.current = false;
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    };
  }, []);

  const send = useCallback((msg: Record<string, unknown>) => {
    if (!openRef.current || !enabledRef.current) return;
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }, []);

  return { send, connected, enabled, viewport, offset, error };
}
