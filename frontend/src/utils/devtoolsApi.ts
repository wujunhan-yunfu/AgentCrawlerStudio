import { api } from "./api";

export interface NetRecord {
  id: string;
  url: string;
  method: string;
  status: number | null;
  statusText: string | null;
  mimeType: string | null;
  type: string;
  started: number;
  finished: number | null;
  duration: number | null;
  size: number | null;
  error: string | null;
  canceled: boolean;
  initiator: string | null;
  requestHeaders: Record<string, string>;
  responseHeaders: Record<string, string> | null;
  postData: string | null;
}

export interface DomNode {
  id: number;
  t: number;
  name: string;
  value: string;
  attrs: Record<string, string>;
  count: number | null;
  children: DomNode[];
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(api(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  const j = (await r.json()) as T;
  if (!r.ok) {
    throw new Error((j as { detail?: string })?.detail || r.statusText);
  }
  return j;
}

export const domApi = {
  tree: () => post<{ ok: boolean; root?: DomNode; error?: string }>("/dom/tree"),
  box: (backendNodeId: number) =>
    post<{ ok: boolean; box?: { x: number; y: number; w: number; h: number } | null; error?: string }>(
      "/dom/box",
      { backend_node_id: backendNodeId }
    ),
};

export const netApi = {
  body: (requestId: string) =>
    post<{ ok: boolean; body?: string; base64_encoded?: boolean; error?: string }>("/network/body", {
      request_id: requestId,
    }),
  clear: () => post<{ ok: boolean }>("/network/clear"),
};

export interface StorageItem {
  key: string;
  value: string;
}

export interface Cookie {
  name: string;
  value: string;
  domain: string;
  path: string;
  expires: number;
  httpOnly: boolean;
  secure: boolean;
  session: boolean;
}

export const storageApi = {
  origin: () => post<{ ok: boolean; origin?: string; error?: string }>("/storage/origin"),
  items: (origin: string, session: boolean) =>
    post<{ ok: boolean; items?: StorageItem[]; error?: string }>("/storage/items", { origin, session }),
  set: (origin: string, session: boolean, key: string, value: string) =>
    post<{ ok: boolean; error?: string }>("/storage/set", { origin, session, key, value }),
  remove: (origin: string, session: boolean, key: string) =>
    post<{ ok: boolean; error?: string }>("/storage/remove", { origin, session, key }),
  cookies: (origin: string) => post<{ ok: boolean; cookies?: Cookie[]; error?: string }>("/storage/cookies", { origin }),
  cookieSet: (origin: string, cookie: Partial<Cookie> & { name: string; value: string }) =>
    post<{ ok: boolean; error?: string }>("/storage/cookie/set", { origin, ...cookie }),
  cookieDelete: (origin: string, name: string) =>
    post<{ ok: boolean; error?: string }>("/storage/cookie/delete", { origin, name }),
  idbDatabases: (origin: string) =>
    post<{ ok: boolean; databases?: string[]; error?: string }>("/storage/idb/databases", { origin }),
  idbStores: (origin: string, database: string) =>
    post<{ ok: boolean; stores?: { name: string; keyPath: unknown; indexes: number }[]; error?: string }>(
      "/storage/idb/stores",
      { origin, database }
    ),
  idbData: (origin: string, database: string, store: string, skip = 0, count = 50) =>
    post<{ ok: boolean; rows?: { key: string; primaryKey: string; value: string }[]; has_more?: boolean; error?: string }>(
      "/storage/idb/data",
      { origin, database, store, skip, count }
    ),
};
