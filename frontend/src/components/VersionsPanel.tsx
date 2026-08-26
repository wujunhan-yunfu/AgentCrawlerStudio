import { useMemo, useState } from "react";
import { codeCommitDetail } from "../utils/api";
import type { CodeCommitSummary } from "../utils/api";
import { diffHunks, diffStatsFromHunks, type DiffHunk } from "../utils/diff";
import type { VersionsState } from "../hooks/useVersions";
import ConflictResolve from "./ConflictResolve";

const shortHash = (h: string) => (h ? h.slice(0, 8) : "-");
const fmtTime = (t: number) => (t ? new Date(t).toLocaleString() : "-");

/**
 * 源码管理面板: 刷新合并/冲突 · 未提交变更 · 提交 · 历史(检出) · 仓库状态。
 * 渲染沿用 VSCode 暗色布局与 oc-diff 类。
 */
export default function VersionsPanel({ versions }: { versions: VersionsState }) {
  const {
    loading,
    ready,
    error,
    crawlerId,
    head,
    headContent,
    commits,
    dirty,
    mergeState,
    mergeResult,
    author,
    persistWarning,
  } = versions;

  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [openDiff, setOpenDiff] = useState(false);
  const [expanded, setExpanded] = useState<{ id: string; from: string; to: string } | null>(
    null,
  );
  const [loadErr, setLoadErr] = useState<string | null>(null);

  const wipStats = useMemo(() => {
    if (headContent == null) return { add: 0, del: 0 };
    const h = diffHunks(headContent, versions.code);
    return diffStatsFromHunks(h);
  }, [headContent, versions.code]);

  if (loading && !ready) {
    return <div className="vc-status">加载中...</div>;
  }

  const doCommit = async () => {
    if (!message.trim() || busy) return;
    setBusy(true);
    try {
      await versions.commit(message.trim());
      setMessage("");
    } catch {
      /* 错误已在面板顶部展示 */
    } finally {
      setBusy(false);
    }
  };

  const doCheckout = async (commitId: string) => {
    if (busy) return;
    if (
      !window.confirm(
        "检出将用该版本覆盖当前工作区(HEAD 不变), 当前未提交变更会被替换。继续?",
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await versions.checkout(commitId);
    } catch {
      /* 错误已在面板顶部展示 */
    } finally {
      setBusy(false);
    }
  };

  const toggleCommit = async (c: CodeCommitSummary) => {
    if (expanded?.id === c.commit_id) {
      setExpanded(null);
      return;
    }
    setLoadErr(null);
    try {
      const cur = await codeCommitDetail(c.commit_id, crawlerId);
      let from = "";
      if (c.parent) {
        const p = await codeCommitDetail(c.parent, crawlerId);
        from = p.content;
      }
      setExpanded({ id: c.commit_id, from, to: cur.content });
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="versions">
      {error ? <div className="vc-error">错误: {error}</div> : null}
      {loadErr ? <div className="vc-error">{loadErr}</div> : null}

      {mergeState === "merged" && mergeResult ? (
        <div className="vc-merge-banner">
          <span>已合并暂存草稿与刷新后的最新变更</span>
          <button className="primary" onClick={versions.acceptMerged}>
            知道了
          </button>
        </div>
      ) : null}

      {mergeState === "conflict" && mergeResult ? (
        <ConflictResolve segments={mergeResult.segments} onApply={versions.applyResolution} />
      ) : null}

      <section className="vc-section">
        <div className="vc-section-head">
          <span className="vc-section-title">未提交变更</span>
          {dirty ? (
            <span className="vc-dirty">
              *{wipStats.add + wipStats.del} 变更(+{wipStats.add} −{wipStats.del})
            </span>
          ) : (
            <span className="vc-clean">与最新提交一致</span>
          )}
          {dirty ? (
            <button className="vc-link" onClick={() => setOpenDiff((o) => !o)}>
              {openDiff ? "收起" : "查看差异"}
            </button>
          ) : null}
        </div>
        {dirty && openDiff ? <DiffBlock from={headContent ?? ""} to={versions.code} /> : null}
      </section>

      <section className="vc-section">
        <div className="vc-section-head">
          <span className="vc-section-title">提交</span>
        </div>
        <div className="vc-commit-row">
          <input
            className="vc-input"
            value={message}
            placeholder="提交信息"
            maxLength={200}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void doCommit();
            }}
          />
          <button
            className="primary"
            disabled={!dirty || !message.trim() || busy}
            onClick={() => void doCommit()}
          >
            提交
          </button>
        </div>
        <div className="vc-commit-author">
          <span>提交人</span>
          <input
            className="vc-input"
            value={author}
            placeholder="unknown"
            onChange={(e) => versions.setAuthor(e.target.value)}
          />
        </div>
      </section>

      <section className="vc-section">
        <div className="vc-section-head">
          <span className="vc-section-title">历史</span>
          <button className="vc-link" onClick={() => void versions.refresh()}>
            刷新
          </button>
        </div>
        {commits.length === 0 ? (
          <div className="vc-empty">暂无提交</div>
        ) : (
          <ul className="vc-commits">
            {commits.map((c) => (
              <li key={c.commit_id} className="vc-commit">
                <div className="vc-commit-head">
                  <span className="vc-commit-hash" title={c.commit_id}>
                    {shortHash(c.commit_id)}
                  </span>
                  <span className="vc-commit-msg">{c.message}</span>
                  <span className="oc-diff-stats">
                    <span className="stat-add">+{c.stat.add}</span>
                    <span className="stat-del">−{c.stat.del}</span>
                  </span>
                </div>
                <div className="vc-commit-meta">
                  {c.author || "unknown"} · {fmtTime(c.created_at)}
                </div>
                <div className="vc-commit-actions">
                  <button className="vc-link" onClick={() => void toggleCommit(c)}>
                    {expanded?.id === c.commit_id ? "收起差异" : "查看差异"}
                  </button>
                  <button className="vc-link" onClick={() => void doCheckout(c.commit_id)}>
                    检出
                  </button>
                </div>
                {expanded?.id === c.commit_id ? (
                  <DiffBlock from={expanded.from} to={expanded.to} />
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="vc-section">
        <div className="vc-section-head">
          <span className="vc-section-title">仓库状态</span>
        </div>
        <div className="vc-repo">
          <div>
            crawler_id: <span className="vc-code">{crawlerId || "default"}</span>
          </div>
          <div>
            HEAD:{" "}
            {head ? (
              <span className="vc-code" title={head.commit_id}>
                {shortHash(head.commit_id)}
              </span>
            ) : (
              <span className="vc-muted">(空仓库)</span>
            )}
            {head ? <span className="vc-muted"> · {head.message}</span> : null}
          </div>
          {persistWarning ? (
            <div className="vc-warn">草稿未保存(本地存储容量不足)</div>
          ) : (
            <div className="vc-muted">本地草稿已持久化</div>
          )}
        </div>
      </section>
    </div>
  );
}

function DiffBlock({ from, to }: { from: string; to: string }) {
  const { hunks, stats } = useMemo(() => {
    const h = diffHunks(from, to, 3);
    return { hunks: h, stats: diffStatsFromHunks(h) };
  }, [from, to]);

  if (stats.add === 0 && stats.del === 0) {
    return <div className="vc-muted vc-pad">无差异</div>;
  }

  return (
    <div className="oc-diff">
      {hunks.map((h, i) => (
        <HunkView key={i} hunk={h} />
      ))}
    </div>
  );
}

function HunkView({ hunk }: { hunk: DiffHunk }) {
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