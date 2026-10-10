import { useCallback, useEffect, useState } from "react";
import { storageApi, type Cookie, type StorageItem } from "../utils/devtoolsApi";
import { api } from "../utils/api";
import { useI18n } from "../i18n";

interface Props {
  // 占位
}

function EditableText({
  value,
  onSave,
  title,
}: {
  value: string;
  onSave: (v: string) => Promise<void>;
  title?: string;
}) {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setEditing(false);
    if (draft === value) return;
    setBusy(true);
    try {
      await onSave(draft);
    } finally {
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <input
        className="storage-edit"
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => void save()}
        onKeyDown={(e) => {
          if (e.key === "Enter") void save();
          if (e.key === "Escape") setEditing(false);
        }}
      />
    );
  }
  return (
    <span
      className="storage-cell"
      title={busy ? t("appP.saving") : (title ?? t("appP.dblclick"))}
      onDoubleClick={() => {
        setDraft(value);
        setEditing(true);
      }}
    >
      {value}
    </span>
  );
}

function StorageTable({
  origin,
  session,
}: {
  origin: string;
  session: boolean;
}) {
  const { t } = useI18n();
  const [items, setItems] = useState<StorageItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [newKey, setNewKey] = useState("");

  const reload = useCallback(async () => {
    const r = await storageApi.items(origin, session);
    if (r.ok) {
      setItems(r.items ?? []);
      setError(null);
    } else {
      setError(r.error ?? t("appP.readFailed"));
    }
  }, [origin, session, t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const add = async () => {
    if (!newKey.trim()) return;
    await storageApi.set(origin, session, newKey.trim(), "");
    setNewKey("");
    void reload();
  };

  return (
    <div className="storage-table-wrap">
      {error ? <div className="bad">{error}</div> : null}
      <table className="net-table storage-table">
        <thead>
          <tr>
            <th>Key</th>
            <th>Value</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((it) => (
            <tr key={it.key}>
              <td className="storage-key">
                <EditableText
                  value={it.key}
                  onSave={async (k) => {
                    if (k !== it.key) {
                      await storageApi.set(origin, session, k, it.value);
                      await storageApi.remove(origin, session, it.key);
                      void reload();
                    }
                  }}
                />
              </td>
              <td className="storage-val">
                <EditableText
                  value={it.value}
                  onSave={async (v) => {
                    await storageApi.set(origin, session, it.key, v);
                    void reload();
                  }}
                />
              </td>
              <td>
                <button
                  className="ghost small"
                  onClick={async () => {
                    await storageApi.remove(origin, session, it.key);
                    void reload();
                  }}
                >
                  {t("common.delete")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="storage-add">
        <input
          className="storage-key"
          placeholder={t("appP.newKey")}
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void add();
          }}
        />
        <button className="ghost" onClick={() => void add()}>{t("appP.add")}</button>
      </div>
    </div>
  );
}

function CookieTable({ origin }: { origin: string }) {
  const { t } = useI18n();
  const [cookies, setCookies] = useState<Cookie[]>([]);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    const r = await storageApi.cookies(origin);
    if (r.ok) {
      setCookies(r.cookies ?? []);
      setError(null);
    } else {
      setError(r.error ?? t("appP.readFailed"));
    }
  }, [origin, t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return (
    <div className="storage-table-wrap">
      {error ? <div className="bad">{error}</div> : null}
      <table className="net-table storage-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Value</th>
            <th>Domain</th>
            <th>Path</th>
            <th>HttpOnly</th>
            <th>Secure</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {cookies.map((c) => (
            <tr key={c.name + c.domain + c.path}>
              <td className="storage-key">
                <EditableText
                  value={c.name}
                  onSave={async (n) => {
                    if (n !== c.name) {
                      await storageApi.cookieSet(origin, { ...c, name: n });
                      await storageApi.cookieDelete(origin, c.name);
                      void reload();
                    }
                  }}
                />
              </td>
              <td className="storage-val">
                <EditableText
                  value={c.value}
                  onSave={async (v) => {
                    await storageApi.cookieSet(origin, { ...c, value: v });
                    void reload();
                  }}
                />
              </td>
              <td>{c.domain}</td>
              <td>{c.path}</td>
              <td>{c.httpOnly ? "✔" : ""}</td>
              <td>{c.secure ? "✔" : ""}</td>
              <td>
                <button
                  className="ghost small"
                  onClick={async () => {
                    await storageApi.cookieDelete(origin, c.name);
                    void reload();
                  }}
                >
                  {t("common.delete")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function IdbPanel({ origin }: { origin: string }) {
  const { t } = useI18n();
  const [dbs, setDbs] = useState<string[]>([]);
  const [stores, setStores] = useState<{ name: string }[]>([]);
  const [db, setDb] = useState<string | null>(null);
  const [store, setStore] = useState<string | null>(null);
  const [rows, setRows] = useState<{ key: string; primaryKey: string; value: string }[]>([]);
  const [skip, setSkip] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  const loadDbs = useCallback(async () => {
    const r = await storageApi.idbDatabases(origin);
    if (r.ok) setDbs(r.databases ?? []);
  }, [origin]);

  useEffect(() => {
    void loadDbs();
  }, [loadDbs]);

  const openDb = async (name: string) => {
    setDb(name);
    setStore(null);
    setRows([]);
    const r = await storageApi.idbStores(origin, name);
    if (r.ok) setStores(r.stores ?? []);
  };

  const openStore = async (name: string, skipCount = 0) => {
    setStore(name);
    setSkip(skipCount);
    const r = await storageApi.idbData(origin, db!, name, skipCount, 50);
    if (r.ok) {
      setRows(skipCount === 0 ? r.rows ?? [] : [...rows, ...(r.rows ?? [])]);
      setHasMore(r.has_more ?? false);
    }
  };

  return (
    <div className="idb-panel">
      <div className="idb-side">
        <div className="idb-side-title">{t("appP.databases")}</div>
        {dbs.length === 0 ? <div className="hint">{t("appP.noDatabases")}</div> : null}
        {dbs.map((d) => (
          <div
            key={d}
            className={`idb-item ${db === d ? "active" : ""}`}
            onClick={() => void openDb(d)}
          >
            {d}
          </div>
        ))}
      </div>
      <div className="idb-main">
        {db ? (
          <>
            <div className="idb-side-title">{t("appP.objectStores", { db })}</div>
            {stores.length === 0 ? <div className="hint">{t("appP.noStores")}</div> : null}
            {stores.map((s) => (
              <div
                key={s.name}
                className={`idb-item ${store === s.name ? "active" : ""}`}
                onClick={() => void openStore(s.name)}
              >
                {s.name}
              </div>
            ))}
          </>
        ) : (
          <div className="hint">{t("appP.selectDb")}</div>
        )}
        {store ? (
          <div className="idb-data">
            <table className="net-table storage-table">
              <thead>
                <tr>
                  <th>Key</th>
                  <th>{t("appP.primaryKey")}</th>
                  <th>{t("appP.value")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>
                    <td className="storage-key">{r.key}</td>
                    <td>{r.primaryKey}</td>
                    <td className="storage-val">{r.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {hasMore ? (
              <button className="ghost" onClick={() => void openStore(store, skip + 50)}>
                {t("appP.loadMore")}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default function ApplicationPanel(_props: Props) {
  const { t } = useI18n();
  const [origin, setOrigin] = useState<string | null>(null);
  const [section, setSection] = useState<"local" | "session" | "cookies" | "idb">("local");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      const r = await storageApi.origin();
      if (r.ok) setOrigin(r.origin ?? null);
      else setError(r.error ?? t("appP.originFailed"));
    })();
    // 存储变更实时刷新
    let ws: WebSocket | null = null;
    let closed = false;
    let retryTimer: number | undefined;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}${api("/ws/storage")}`);
      ws.onclose = () => {
        if (!closed) retryTimer = window.setTimeout(connect, 1000);
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
  }, [t]);

  if (error) return <div className="bad net-hint">{error}</div>;
  if (!origin) return <div className="hint net-hint">{t("appP.originLoading")}</div>;

  const nav = (
    <div className="app-side">
      <div className="app-side-origin" title={origin}>{origin}</div>
      <div className={`app-item ${section === "local" ? "active" : ""}`} onClick={() => setSection("local")}>{t("appP.localStorage")}</div>
      <div className={`app-item ${section === "session" ? "active" : ""}`} onClick={() => setSection("session")}>{t("appP.sessionStorage")}</div>
      <div className={`app-item ${section === "cookies" ? "active" : ""}`} onClick={() => setSection("cookies")}>Cookies</div>
      <div className={`app-item ${section === "idb" ? "active" : ""}`} onClick={() => setSection("idb")}>IndexedDB</div>
    </div>
  );

  return (
    <div className="app-panel">
      {nav}
      <div className="app-main">
        <div className="app-section-title">
          {section === "local" && t("appP.localStorage")}
          {section === "session" && t("appP.sessionStorage")}
          {section === "cookies" && "Cookies"}
          {section === "idb" && "IndexedDB"}
        </div>
        {section === "local" && <StorageTable origin={origin} session={false} />}
        {section === "session" && <StorageTable origin={origin} session={true} />}
        {section === "cookies" && <CookieTable origin={origin} />}
        {section === "idb" && <IdbPanel origin={origin} />}
      </div>
    </div>
  );
}
