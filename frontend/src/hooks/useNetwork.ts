import { useCallback, useEffect, useRef, useState } from "react";
import type { NetRecord } from "../utils/devtoolsApi";
import { api } from "../utils/api";

const MAX_RECORDS = 500;

interface NetEvent {
  op: string;
  record?: NetRecord;
}

export function useNetwork(opts?: { preserveLogRef?: React.RefObject<boolean> }): {
  records: NetRecord[];
  connected: boolean;
  clear: () => void;
} {
  const [records, setRecords] = useState<NetRecord[]>([]);
  const [connected, setConnected] = useState(false);
  const pendingRef = useRef<NetEvent[]>([]);
  const timerRef = useRef<number | undefined>(undefined);
  const preserveRef = opts?.preserveLogRef;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retryTimer: number | undefined;

    const flush = () => {
      timerRef.current = undefined;
      if (pendingRef.current.length === 0) return;
      const batch = pendingRef.current;
      pendingRef.current = [];
      setRecords((prev) => {
        let list = prev;
        for (const ev of batch) {
          if (ev.op === "clear") {
            list = [];
          } else if (ev.op === "nav") {
            if (!(preserveRef?.current ?? false)) list = [];
          } else if (ev.record) {
            const rid = ev.record.id;
            const idx = list.findIndex((r) => r.id === rid);
            if (idx >= 0) {
              list = list.map((r) => (r.id === rid ? ev.record! : r));
            } else {
              list = [...list, ev.record];
            }
          }
        }
        return list.slice(-MAX_RECORDS);
      });
    };

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}${api("/ws/network")}`);
      ws.onopen = () => setConnected(true);
      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data as string) as { type: string; op: string; record?: NetRecord };
          if (m.type !== "network") return;
          pendingRef.current.push(m);
          if (timerRef.current === undefined) {
            timerRef.current = window.setTimeout(flush, 100);
          }
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retryTimer = window.setTimeout(connect, 1000);
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
      if (timerRef.current) window.clearTimeout(timerRef.current);
      if (retryTimer) window.clearTimeout(retryTimer);
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    };
  }, []);

  const clear = useCallback(() => {
    pendingRef.current = [];
    if (timerRef.current) window.clearTimeout(timerRef.current);
    timerRef.current = undefined;
    setRecords([]);
  }, []);

  return { records, connected, clear };
}
