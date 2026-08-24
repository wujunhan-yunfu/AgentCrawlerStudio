import { useEffect, useState } from "react";
import type { Status } from "../types";
import { api } from "../utils/api";

export function useStatus(pollMs = 2000): Status | null {
  const [status, setStatus] = useState<Status | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await fetch(api("/status"));
        if (r.ok && !cancelled) {
          setStatus((await r.json()) as Status);
        }
      } catch {
        /* 服务未就绪 */
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [pollMs]);

  return status;
}
