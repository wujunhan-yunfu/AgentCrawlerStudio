import { useMemo, useState } from "react";
import { diffHunks, diffStatsFromHunks, type DiffHunk } from "../utils/diff";

interface Props {
  from: string;
  to: string;
}

const CONTEXT = 3;

/**
 * opencode 风格的代码变更卡片: 顶部是 `← Edit <目标>` 块头,
 * 展开后按 git diff 渲染变更行(两侧行号 + 红绿底色 + +/− 符号)。
 */
export default function AgentDiffCard({ from, to }: Props) {
  const [open, setOpen] = useState(true);
  const { hunks, stats } = useMemo(() => {
    const h = diffHunks(from, to, CONTEXT);
    return { hunks: h, stats: diffStatsFromHunks(h) };
  }, [from, to]);

  if (stats.add === 0 && stats.del === 0) return null;

  return (
    <div className={`oc-block oc-diff${open ? " open" : ""}`}>
      <div
        className="oc-block-head"
        role="button"
        tabIndex={0}
        title={open ? "收起" : "展开"}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen((o) => !o);
          }
        }}
      >
        <span className="oc-block-ico">←</span>
        <span className="oc-block-title">Edit 编辑器</span>
        <span className="oc-diff-stats">
          <span className="stat-add">+{stats.add}</span>
          <span className="stat-del">−{stats.del}</span>
        </span>
        <span className="oc-caret">{open ? "▾" : "▸"}</span>
      </div>
      {open ? (
        <div className="oc-block-body">
          <div className="oc-diff">
            {hunks.map((h, i) => (
              <DiffHunkView key={i} hunk={h} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function DiffHunkView({ hunk }: { hunk: DiffHunk }) {
  return (
    <div className="oc-diff-hunk">
      <div className="oc-diff-hunk-head">
        @@ -{hunk.oldStart},{hunk.oldCount} +{hunk.newStart},{hunk.newCount} @@
      </div>
      <div className="oc-diff-hunk-body">
        {hunk.lines.map((line, i) => (
          <div key={i} className={`oc-diff-line ${line.type}`}>
            <span className="oc-diff-no old">{line.oldNo ?? ""}</span>
            <span className="oc-diff-no new">{line.newNo ?? ""}</span>
            <span className="oc-diff-sign">
              {line.type === "add" ? "+" : line.type === "del" ? "−" : " "}
            </span>
            <span className="oc-diff-text">{line.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
