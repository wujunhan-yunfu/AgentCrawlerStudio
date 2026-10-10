import type { PageInfo } from "../types";
import { useI18n } from "../i18n";

export default function PagesList({ pages }: { pages?: PageInfo[] }) {
  const { t } = useI18n();
  if (!pages || pages.length === 0) {
    return (
      <ul className="pages">
        <li>{t("pages.empty")}</li>
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
