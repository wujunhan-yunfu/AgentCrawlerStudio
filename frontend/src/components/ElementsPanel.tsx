import { useCallback, useEffect, useRef, useState } from "react";
import { domApi, type DomNode } from "../utils/devtoolsApi";
import { api } from "../utils/api";
import { useI18n } from "../i18n";

export interface HighlightBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Props {
  onHighlight: (box: HighlightBox | null) => void;
}

function nodeLabel(node: DomNode): React.ReactNode {
  if (node.t === 3) return <span className="el-text">{node.value}</span>;
  if (node.t === 8) return <span className="el-comment">&lt;!--{node.value}--&gt;</span>;
  if (node.t === 9) return <span className="el-doc">#document</span>;
  const tag = node.name.toLowerCase();
  return (
    <span>
      <span className="el-tag">&lt;{tag}</span>
      {node.attrs["id"] ? <span className="el-attr"> id=<span className="el-attr-val">"{node.attrs["id"]}"</span></span> : null}
      {node.attrs["class"] ? <span className="el-attr"> class=<span className="el-attr-val">"{node.attrs["class"]}"</span></span> : null}
      {Object.entries(node.attrs)
        .filter(([k]) => k !== "id" && k !== "class")
        .slice(0, 3)
        .map(([k, v]) => (
          <span className="el-attr" key={k}> {k}=<span className="el-attr-val">"{v}"</span></span>
        ))}
      <span className="el-tag">&gt;</span>
    </span>
  );
}

function TreeNode({
  node,
  depth,
  expanded,
  toggle,
  selectedId,
  onSelect,
}: {
  node: DomNode;
  depth: number;
  expanded: Set<number>;
  toggle: (id: number) => void;
  selectedId: number | null;
  onSelect: (node: DomNode) => void;
}) {
  const hasChildren = node.children.length > 0;
  const isOpen = expanded.has(node.id);
  const isSelected = selectedId === node.id;

  return (
    <div>
      <div
        className={`el-node ${isSelected ? "selected" : ""}`}
        style={{ paddingLeft: depth * 14 }}
        onClick={() => onSelect(node)}
      >
        {hasChildren ? (
          <span
            className={`el-caret ${isOpen ? "open" : ""}`}
            onClick={(e) => {
              e.stopPropagation();
              toggle(node.id);
            }}
          >
            ▸
          </span>
        ) : (
          <span className="el-caret-spacer" />
        )}
        {nodeLabel(node)}
      </div>
      {isOpen
        ? node.children.map((c) => (
            <TreeNode
              key={c.id}
              node={c}
              depth={depth + 1}
              expanded={expanded}
              toggle={toggle}
              selectedId={selectedId}
              onSelect={onSelect}
            />
          ))
        : null}
    </div>
  );
}

function AttrPanel({ node }: { node: DomNode }) {
  const { t } = useI18n();
  const attrs = Object.entries(node.attrs);
  const isText = node.t === 3 || node.t === 8;
  return (
    <div className="el-attr-panel">
      <div className="net-detail-head">
        <span className="net-detail-title">
          {node.t === 1 ? t("elements.element", { name: node.name.toLowerCase() }) : t("elements.node")}
        </span>
      </div>
      {isText ? (
        <pre className="net-pre">{node.value || t("elements.emptyText")}</pre>
      ) : (
        <div className="el-attr-list">
          <div className="el-kv"><span className="k">{t("elements.tag")}</span><span className="v">{node.name}</span></div>
          {attrs.length === 0 ? <div className="hint">{t("elements.noAttr")}</div> : null}
          {attrs.map(([k, v]) => (
            <div className="el-kv" key={k}>
              <span className="k">{k}</span>
              <span className="v">{v}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ElementsPanel({ onHighlight }: Props) {
  const { t } = useI18n();
  const [root, setRoot] = useState<DomNode | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [selected, setSelected] = useState<DomNode | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await domApi.tree();
      if (r.ok && r.root) {
        setRoot(r.root);
        // 默认展开前几层
        const init = new Set<number>();
        const walk = (n: DomNode, depth: number) => {
          if (depth < 2 && n.children.length > 0) {
            init.add(n.id);
            for (const c of n.children) walk(c, depth + 1);
          }
        };
        walk(r.root, 0);
        setExpanded(init);
      } else {
        setError(r.error ?? t("elements.loadFailed"));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
    let ws: WebSocket | null = null;
    let closed = false;
    let retryTimer: number | undefined;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}${api("/ws/dom")}`);
      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data as string) as { type: string; op: string };
          if (m.type === "dom" && m.op === "reload") void load();
        } catch {
          /* ignore */
        }
      };
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
  }, [load]);

  const toggle = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleSelect = async (node: DomNode) => {
    setSelected(node);
    onHighlight(null);
    if (node.t === 1) {
      try {
        const r = await domApi.box(node.id);
        if (r.ok) onHighlight(r.box ?? null);
      } catch {
        /* ignore */
      }
    }
  };

  return (
    <div className="el-panel">
      <div className="net-toolbar">
        <button className="ghost" onClick={() => void load()} disabled={loading}>
          {loading ? t("common.loading") : t("common.refresh")}
        </button>
        <span className="net-count">{root ? t("elements.nodeCount", { count: countNodes(root) }) : ""}</span>
        <span className="net-actions">
          {selected && selected.t === 1 ? (
            <button className="ghost" onClick={() => onHighlight(null)}>{t("elements.unhighlight")}</button>
          ) : null}
        </span>
      </div>
      {error ? <div className="bad net-hint">{error}</div> : null}
      <div className="el-content">
        <div className="el-tree" ref={listRef}>
          {root ? (
            <TreeNode
              node={root}
              depth={0}
              expanded={expanded}
              toggle={toggle}
              selectedId={selected?.id ?? null}
              onSelect={(n) => void handleSelect(n)}
            />
          ) : (
            <div className="hint">{t("elements.loadingDom")}</div>
          )}
        </div>
        {selected ? <AttrPanel node={selected} /> : null}
      </div>
    </div>
  );
}

function countNodes(node: DomNode): number {
  return 1 + node.children.reduce((acc, c) => acc + countNodes(c), 0);
}
