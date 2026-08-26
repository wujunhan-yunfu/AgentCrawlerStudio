import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  agentInfo,
  codeCheckout,
  codeCommit,
  codeCommitDetail,
  codeCommits,
  codeRepo,
  getEditorCode,
  setEditorCode,
  type CodeCommitInfo,
  type CodeCommitSummary,
  type CodeHeadInfo,
} from "../utils/api";
import {
  merge3,
  wholeFileConflict,
  type MergeResult,
  type MergeSegment,
} from "../utils/merge";

export type MergeState = "none" | "merged" | "conflict";
export type ResolutionPick = "mine" | "theirs" | "both";

const DRAFT_PREFIX = "crawlerCode:draft:";
const AUTHOR_PREFIX = "crawlerCode:author:";

interface DraftDoc {
  content: string;
  saved_at: number;
  /** 保存时 HEAD 的 commit_id, 同时是刷新后三方合并的基点(base) */
  head: string | null;
  author: string;
}

function loadJSON<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : fallback;
  } catch {
    return fallback;
  }
}

function loadDraft(cid: string): DraftDoc | null {
  return loadJSON<DraftDoc | null>(`${DRAFT_PREFIX}${cid}`, null);
}

function loadAuthor(cid: string): string {
  return loadJSON<string>(`${AUTHOR_PREFIX}${cid}`, "");
}

export interface VersionsState {
  ready: boolean;
  loading: boolean;
  error: string | null;
  crawlerId: string;
  code: string;
  head: CodeHeadInfo | null;
  headContent: string | null;
  commits: CodeCommitSummary[];
  dirty: boolean;
  mergeState: MergeState;
  mergeResult: MergeResult | null;
  author: string;
  persistWarning: boolean;
  setAuthor: (name: string) => void;
  commit: (message: string) => Promise<void>;
  checkout: (commitId: string) => Promise<void>;
  applyResolution: (picks: ResolutionPick[]) => void;
  resolveAll: (pick: ResolutionPick) => void;
  acceptMerged: () => void;
  refresh: () => Promise<void>;
}

/**
 * 源码版本管理: 工作区草稿(localStorage) + 已提交历史(MongoDB)。
 *
 * 刷新/重开后若「最新代码已变更」(他人提交 / Agent 回写镜像), 与暂存草稿做三方合并:
 * 无冲突自动合并, 有冲突进入解决界面, 绝不在加载时静默覆盖。
 * 浏览器端所有持久化键按 crawler_id 命名空间隔离。
 */
export function useVersions(code: string, onCodeChange: (code: string) => void): VersionsState {
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [crawlerId, setCrawlerId] = useState("");
  const [head, setHead] = useState<CodeHeadInfo | null>(null);
  const [headContent, setHeadContent] = useState<string | null>(null);
  const [commits, setCommits] = useState<CodeCommitSummary[]>([]);
  const [mergeState, setMergeState] = useState<MergeState>("none");
  const [mergeResult, setMergeResult] = useState<MergeResult | null>(null);
  const [author, setAuthorState] = useState("");
  const [persistWarning, setPersistWarning] = useState(false);

  // 草稿基点 commit_id(保存时 HEAD), 用于草稿落盘与下次刷新的三方合并
  const baseHeadRef = useRef<string | null>(null);
  const cidRef = useRef("");
  cidRef.current = crawlerId;
  // author 写入走 ref, 避免 load 期间异步 setState 导致草稿落盘时取到旧值
  const authorRef = useRef("");

  const dirty = useMemo(() => code !== (headContent ?? ""), [code, headContent]);

  const saveDraft = useCallback((content: string, baseHead: string | null) => {
    const cid = cidRef.current;
    if (!cid) return;
    try {
      const doc: DraftDoc = {
        content,
        saved_at: Date.now(),
        head: baseHead,
        author: authorRef.current,
      };
      localStorage.setItem(`${DRAFT_PREFIX}${cid}`, JSON.stringify(doc));
      setPersistWarning(false);
    } catch {
      setPersistWarning(true);
    }
  }, []);

  const syncMirror = useCallback((content: string) => {
    void setEditorCode(content).catch(() => {});
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const info = await agentInfo();
      const cid = (info.crawler_id || "default").trim() || "default";
      setCrawlerId(cid);
      cidRef.current = cid;

      const saved = loadDraft(cid);
      const savedAuthor = loadAuthor(cid) || saved?.author || "";
      if (savedAuthor) {
        setAuthorState(savedAuthor);
        authorRef.current = savedAuthor;
      }

      const [repoRes, listRes] = await Promise.all([
        codeRepo(cid).catch(() => null),
        codeCommits(cid).catch(() => null),
      ]);
      const repoHead = repoRes?.head ?? null;
      setHead(repoHead);
      setCommits(listRes?.commits ?? []);

      const headContentNow =
        repoHead && repoHead.commit_id
          ? ((await codeCommitDetail(repoHead.commit_id, cid).catch(() => null))?.content ?? null)
          : null;
      setHeadContent(headContentNow);

      if (!saved || !saved.content) {
        // 无草稿: 取 HEAD 作为工作区, 否则空编辑器
        baseHeadRef.current = repoHead?.commit_id ?? null;
        if (headContentNow != null && headContentNow !== code) onCodeChange(headContentNow);
        setMergeState("none");
        setMergeResult(null);
        return;
      }

      // 有草稿 → 刷新后三方合并
      baseHeadRef.current = saved.head ?? null;
      let baseContent: string | null = "";
      if (saved.head) {
        baseContent =
          (await codeCommitDetail(saved.head, cid).catch(() => null))?.content ?? null;
      }

      if (baseContent === null) {
        // 基点提交缺失(reset/清理) → 整文件冲突, 手动选择
        baseContent = "";
        setMergeState("conflict");
        setMergeResult(wholeFileConflict(saved.content, headContentNow ?? ""));
        return;
      }

      // 确定「最新权威代码」: 优先他人新提交的 HEAD; 否则 Agent 回写的后端镜像
      let theirs = baseContent;
      if (repoHead && repoHead.commit_id !== saved.head) {
        theirs = headContentNow ?? baseContent;
      } else {
        const mirror = await getEditorCode().catch(() => null);
        if (mirror?.code && mirror.code !== baseContent && mirror.code !== saved.content) {
          theirs = mirror.code;
        }
      }

      if (theirs === baseContent) {
        // 无外部变更 → 直接恢复草稿
        baseHeadRef.current = saved.head ?? null;
        if (saved.content !== code) onCodeChange(saved.content);
        setMergeState("none");
        setMergeResult(null);
        return;
      }

      const merged = merge3(baseContent, saved.content, theirs);
      if (merged.conflictCount === 0) {
        baseHeadRef.current = repoHead?.commit_id ?? saved.head ?? null;
        if (merged.code !== code) onCodeChange(merged.code);
        saveDraft(merged.code, baseHeadRef.current);
        syncMirror(merged.code);
        setMergeState("merged");
        setMergeResult(merged);
      } else {
        setMergeState("conflict");
        setMergeResult(merged);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setMergeState("none");
      setMergeResult(null);
    } finally {
      setLoading(false);
      setReady(true);
    }
  }, [code, onCodeChange, saveDraft, syncMirror]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 自动保存: 工作区变化防抖 500ms 写 localStorage(配额异常降级提示)
  useEffect(() => {
    if (!ready || !crawlerId) return;
    const t = setTimeout(() => {
      saveDraft(code, baseHeadRef.current);
    }, 500);
    return () => clearTimeout(t);
  }, [code, crawlerId, ready, saveDraft]);

  const commit = useCallback(
    async (message: string) => {
      if (!crawlerId) return;
      try {
        const res = await codeCommit(message, code, author, crawlerId);
        const c = res.commit;
        setHead({ commit_id: c.commit_id, message: c.message, created_at: c.created_at });
        setHeadContent(c.content);
        baseHeadRef.current = c.commit_id;
        saveDraft(c.content, c.commit_id);
        syncMirror(c.content);
        const listRes = await codeCommits(crawlerId).catch(() => null);
        if (listRes) setCommits(listRes.commits);
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        throw e;
      }
    },
    [code, crawlerId, author, saveDraft, syncMirror],
  );

  const checkout = useCallback(
    async (commitId: string) => {
      if (!crawlerId) return;
      try {
        const res = await codeCheckout(commitId, crawlerId);
        onCodeChange(res.code);
        saveDraft(res.code, baseHeadRef.current);
        setMergeState("none");
        setMergeResult(null);
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        throw e;
      }
    },
    [crawlerId, onCodeChange, saveDraft],
  );

  const applyResolution = useCallback(
    (picks: ResolutionPick[]) => {
      const seg = mergeResult;
      if (!seg) return;
      const parts: string[] = [];
      let ci = 0;
      for (const s of seg.segments) {
        if (s.type === "conflict") {
          const pick = picks[ci] ?? "mine";
          ci++;
          if (pick === "mine") parts.push(...(s.mine ?? []));
          else if (pick === "theirs") parts.push(...(s.theirs ?? []));
          else parts.push(...(s.mine ?? []), ...(s.theirs ?? []));
        } else {
          parts.push(...s.lines);
        }
      }
      const resolved = parts.join("\n");
      onCodeChange(resolved);
      baseHeadRef.current = head?.commit_id ?? baseHeadRef.current;
      saveDraft(resolved, baseHeadRef.current);
      syncMirror(resolved);
      setMergeState("none");
      setMergeResult(null);
    },
    [mergeResult, head, onCodeChange, saveDraft, syncMirror],
  );

  const resolveAll = useCallback(
    (pick: ResolutionPick) => {
      const n = mergeResult?.conflictCount ?? 0;
      applyResolution(Array.from({ length: n }, () => pick));
    },
    [mergeResult, applyResolution],
  );

  const acceptMerged = useCallback(() => {
    setMergeState("none");
    setMergeResult(null);
  }, []);

  const refresh = useCallback(async () => {
    await load();
  }, [load]);

  return {
    ready,
    loading,
    error,
    crawlerId,
    code,
    head,
    headContent,
    commits,
    dirty,
    mergeState,
    mergeResult,
    author,
    persistWarning,
    setAuthor: (name: string) => {
      const n = name.trim();
      setAuthorState(n);
      authorRef.current = n;
      if (cidRef.current) {
        try {
          localStorage.setItem(`${AUTHOR_PREFIX}${cidRef.current}`, JSON.stringify(n));
        } catch {
          /* 隐私模式等 localStorage 不可用时静默降级 */
        }
      }
    },
    commit,
    checkout,
    applyResolution,
    resolveAll,
    acceptMerged,
    refresh,
  };
}

export type { CodeCommitInfo };
export type { MergeSegment };