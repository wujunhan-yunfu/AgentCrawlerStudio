import type { PageInfo } from "../types";

export default function PagesList({ pages }: { pages?: PageInfo[] }) {
  if (!pages || pages.length === 0) {
    return (
      <ul className="pages">
        <li>无页面</li>
      </ul>
    );
  }
  return (
    <ul className="pages">
      {pages.map((p) => (
        <li key={p.id || p.url} title={p.url}>
          {p.title || p.url}
        </li>
      ))}
    </ul>
  );
}
