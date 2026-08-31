import { useState } from "react";
import type { ConsoleItem } from "../hooks/useConsole";
import { api } from "../utils/api";

interface PropEntry {
  name: string;
  item: ConsoleItem;
}

export function parseStyle(css: string): React.CSSProperties | undefined {
  const style: React.CSSProperties = {};
  const color = css.match(/color:\s*([^;]+)/i);
  const bg = css.match(/background(?:-color)?:\s*([^;]+)/i);
  const weight = css.match(/font-weight:\s*([^;]+)/i);
  const size = css.match(/font-size:\s*([^;]+)/i);
  if (color) style.color = color[1].trim();
  if (bg) style.backgroundColor = bg[1].trim();
  if (weight) style.fontWeight = weight[1].trim() as React.CSSProperties["fontWeight"];
  if (size) style.fontSize = size[1].trim();
  return Object.keys(style).length ? style : undefined;
}

function valueClass(t?: string | null): string {
  switch (t) {
    case "number":
    case "boolean":
    case "bigint":
      return "cnum";
    case "string":
      return "cstr";
    case "null":
    case "undefined":
      return "cnul";
    default:
      return "cplain";
  }
}

function Preview({ prev }: { prev: { n: string; v: string; t?: string }[] }) {
  return (
    <span className="objv-prev">
      {" { "}
      {prev.map((p, i) => (
        <span key={i}>
          <span className="objv-key">{p.n}</span>
          <span className="objv-colon">: </span>
          <span className={`objv-text ${valueClass(p.t)}`}>{p.v}</span>
          {i < prev.length - 1 ? <span>, </span> : null}
        </span>
      ))}
      {" … }"}
    </span>
  );
}

export default function ObjectTree({ item }: { item: ConsoleItem }) {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [children, setChildren] = useState<PropEntry[] | null>(null);

  const isExpandable = item.k === "obj" && Boolean(item.oid);

  const toggle = async () => {
    if (!isExpandable) return;
    if (expanded) {
      setExpanded(false);
      return;
    }
    if (children === null) {
      setLoading(true);
      setError(null);
      try {
        const r = await fetch(api("/console/properties"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ object_id: item.oid }),
        });
        const j = (await r.json()) as { ok: boolean; props?: PropEntry[]; error?: string };
        if (j.ok) setChildren(j.props ?? []);
        else setError(j.error ?? "展开失败");
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    }
    setExpanded(true);
  };

  if (item.k === "text" || (item.k === "obj" && !item.oid)) {
    return (
      <span
        className={`objv-text ${valueClass(item.t)}`}
        style={item.style ? parseStyle(item.style) : undefined}
      >
        {item.v ?? ""}
      </span>
    );
  }

  return (
    <span className="objv">
      <span
        className={`objv-caret ${expanded ? "open" : ""}`}
        onClick={(e) => {
          e.stopPropagation();
          void toggle();
        }}
      >
        ▸
      </span>
      <span className="objv-label" onClick={() => void toggle()}>
        <span className="objv-name">{item.v ?? ""}</span>
        {!expanded && item.prev && item.prev.length > 0 ? <Preview prev={item.prev} /> : null}
      </span>
      {loading ? <span className="objv-loading"> 加载中...</span> : null}
      {error ? <span className="objv-error"> ({error})</span> : null}
      {expanded && children !== null ? (
        <span className="objv-children">
          {children.length === 0 ? <span className="objv-empty"> (无属性)</span> : null}
          {children.map((p, i) => (
            <span key={i} className="objv-row">
              <span className="objv-key">{p.name}</span>
              <span className="objv-colon">: </span>
              <ObjectTree item={p.item} />
            </span>
          ))}
        </span>
      ) : null}
    </span>
  );
}
