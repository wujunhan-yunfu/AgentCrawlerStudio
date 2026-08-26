import { useState } from "react";
import type { MergeSegment } from "../utils/merge";
import type { ResolutionPick } from "../hooks/useVersions";

interface Props {
  segments: MergeSegment[];
  onApply: (picks: ResolutionPick[]) => void;
}

/**
 * 刷新后合并冲突解决: 逐处选择保留暂存 / 保留最新 / 保留两者。
 * 渲染复用 oc-diff 类(暂存=删除色, 最新=新增色), 便于直观对比。
 */
export default function ConflictResolve({ segments, onApply }: Props) {
  const conflicts = segments.filter((s) => s.type === "conflict");
  const [picks, setPicks] = useState<ResolutionPick[]>(() =>
    conflicts.map(() => "mine"),
  );

  if (conflicts.length === 0) return null;

  const setPick = (i: number, p: ResolutionPick) =>
    setPicks((prev) => prev.map((x, idx) => (idx === i ? p : x)));

  const all = (p: ResolutionPick) => setPicks(conflicts.map(() => p));

  return (
    <div className="vc-conflict">
      <div className="vc-conflict-title">
        暂存草稿与最新代码存在 {conflicts.length} 处冲突, 请逐处选择保留内容
      </div>
      <div className="vc-conflict-actions">
        <button onClick={() => all("mine")}>全部保留暂存</button>
        <button onClick={() => all("theirs")}>全部保留最新</button>
        <button onClick={() => all("both")}>全部保留两者</button>
      </div>
      {conflicts.map((s, i) => (
        <div key={i} className="vc-conflict-block">
          <div className="vc-conflict-picker">
            <span className="vc-conflict-label">冲突 #{i + 1}:</span>
            <button
              className={picks[i] === "mine" ? "active" : ""}
              onClick={() => setPick(i, "mine")}
            >
              保留暂存
            </button>
            <button
              className={picks[i] === "theirs" ? "active" : ""}
              onClick={() => setPick(i, "theirs")}
            >
              保留最新
            </button>
            <button
              className={picks[i] === "both" ? "active" : ""}
              onClick={() => setPick(i, "both")}
            >
              保留两者
            </button>
          </div>
          <div className="oc-diff">
            <div className="oc-diff-hunk">
              <div className="oc-diff-hunk-head">暂存(本地) vs 最新(远端)</div>
              <div className="oc-diff-hunk-body">
                {(s.mine ?? []).map((l, li) => (
                  <div key={`m${li}`} className="oc-diff-line del">
                    <span className="oc-diff-no old">{li + 1}</span>
                    <span className="oc-diff-no new" />
                    <span className="oc-diff-sign">−</span>
                    <span className="oc-diff-text">{l}</span>
                  </div>
                ))}
                <div className="oc-diff-line ctx">
                  <span className="oc-diff-no old" />
                  <span className="oc-diff-no new" />
                  <span className="oc-diff-sign"> </span>
                  <span className="oc-diff-text">=======</span>
                </div>
                {(s.theirs ?? []).map((l, li) => (
                  <div key={`t${li}`} className="oc-diff-line add">
                    <span className="oc-diff-no old" />
                    <span className="oc-diff-no new">{li + 1}</span>
                    <span className="oc-diff-sign">+</span>
                    <span className="oc-diff-text">{l}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      ))}
      <div className="vc-conflict-confirm">
        <button className="primary" onClick={() => onApply(picks)}>
          应用选择并写回编辑器
        </button>
      </div>
    </div>
  );
}