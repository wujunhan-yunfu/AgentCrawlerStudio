import { useEffect, useRef, useState } from "react";
import { api } from "../utils/api";

export interface ConsoleItem {
  k: "text" | "obj";
  t?: string | null;
  v?: string | null;
  oid?: string | null;
  ou?: string | null;
  sub?: string | null;
  cls?: string | null;
  style?: string | null;
  prev?: { n: string; v: string; t?: string }[] | null;
}

export interface ConsoleMessage {
  type: string;
  kind: string;
  level: string;
  text: string;
  items: ConsoleItem[];
  url?: string | null;
  line?: number | null;
  stack?: string | null;
  ts: number;
  group?: number;
  table?: unknown;
}

const MAX_MESSAGES = 500;

export function useConsole(): {
  messages: ConsoleMessage[];
  connected: boolean;
  clear: () => void;
  evaluate: (expression: string) => Promise<void>;
} {
  const [messages, setMessages] = useState<ConsoleMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const bufRef = useRef<ConsoleMessage[]>([]);
  const flushTimerRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retryTimer: number | undefined;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}${api("/ws/console")}`);
      ws.onopen = () => setConnected(true);
      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data as string) as ConsoleMessage;
          if (m.type !== "console") return;
          bufRef.current.push(m);
          if (bufRef.current.length >= 20) flush();
          else if (flushTimerRef.current === undefined) {
            flushTimerRef.current = window.setTimeout(flush, 100);
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
      if (flushTimerRef.current) window.clearTimeout(flushTimerRef.current);
      if (retryTimer) window.clearTimeout(retryTimer);
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const flush = () => {
    if (flushTimerRef.current) window.clearTimeout(flushTimerRef.current);
    flushTimerRef.current = undefined;
    if (bufRef.current.length === 0) return;
    const batch = bufRef.current;
    bufRef.current = [];
    setMessages((prev) => {
      let list = prev;
      for (const m of batch) {
        if (m.kind === "clear") list = [];
        else list = [...list, m].slice(-MAX_MESSAGES);
      }
      return list;
    });
  };

  const pushLocal = (m: ConsoleMessage) => {
    bufRef.current.push(m);
    flush();
  };

  const clear = () => {
    bufRef.current = [];
    if (flushTimerRef.current) window.clearTimeout(flushTimerRef.current);
    flushTimerRef.current = undefined;
    setMessages([]);
  };

  const evaluate = async (expression: string) => {
    const now = Date.now() / 1000;
    pushLocal({
      type: "console",
      kind: "eval-input",
      level: "input",
      text: expression,
      items: [{ k: "text", t: "str", v: expression }],
      ts: now,
      group: 0,
    });
    let result: ConsoleMessage;
    try {
      const r = await fetch(api("/console/eval"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expression }),
      });
      const j = (await r.json()) as { ok: boolean; item?: ConsoleItem; error?: string; stack?: string | null };
      if (j.ok && j.item) {
        result = {
          type: "console",
          kind: "eval",
          level: "log",
          text: j.item.v ?? "",
          items: [j.item],
          ts: Date.now() / 1000,
          group: 0,
        };
      } else {
        const err = j.error ?? "求值失败";
        result = {
          type: "console",
          kind: "eval",
          level: "error",
          text: err,
          items: [{ k: "text", t: "str", v: err }],
          stack: j.stack ?? null,
          ts: Date.now() / 1000,
          group: 0,
        };
      }
    } catch (e) {
      const err = e instanceof Error ? e.message : String(e);
      result = {
        type: "console",
        kind: "eval",
        level: "error",
        text: err,
        items: [{ k: "text", t: "str", v: err }],
        stack: null,
        ts: Date.now() / 1000,
        group: 0,
      };
    }
    pushLocal(result);
  };

  return { messages, connected, clear, evaluate };
}
