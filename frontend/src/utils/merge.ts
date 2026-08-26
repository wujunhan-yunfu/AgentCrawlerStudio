/**
 * 三方行级合并(merge3): 用于「刷新后代码已变更」时合并浏览器暂存草稿与最新代码。
 *
 * 语义对齐 Git 三路合并:
 * - base   = 草稿的基点(草稿.head 对应提交的内容)
 * - mine   = 浏览器暂存的草稿(本地未提交变更)
 * - theirs = 最新权威代码(他人新提交的 HEAD / Agent 回写的后端镜像)
 *
 * 算法: 用 LCS 把两侧改动对齐到 base, 每个变更点映射到「坐标」(偶数=base 行,
 * 奇数=行间插入槽)。被删除的 base 行形成区段(含其边界插入槽), 孤立插入形成
 * 零宽区段。对每个区段:
 * - 只有一方改动 → 取改动方;
 * - 双方改动但结果一致 → 取该结果;
 * - 双方改动且结果不同 → 冲突, 输出 git 风格标记 <<<<<<< / ======= / >>>>>>>
 *   并在 segments 中以 conflict 段暴露双方内容, 供前端冲突解决界面使用。
 */

export type MergeSegmentType = "context" | "mine" | "theirs" | "conflict";

export interface MergeSegment {
  type: MergeSegmentType;
  /** 该段落解析后的输出行(conflict 段为空, 内容在 mine/theirs) */
  lines: string[];
  /** conflict 段: 暂存(本地)内容 */
  mine?: string[];
  /** conflict 段: 最新(远端)内容 */
  theirs?: string[];
}

export interface MergeResult {
  /** 合并后的完整代码(冲突处以 <<<<<<< / ======= / >>>>>>> 标记) */
  code: string;
  /** 结构化段落, 供 diff/冲突渲染 */
  segments: MergeSegment[];
  conflictCount: number;
}

const LCS_LIMIT = 4_000_000;

type RawOp = { t: "ctx" | "del" | "add"; text: string };

function splitLines(text: string): string[] {
  if (!text) return [];
  return text.replace(/\r\n/g, "\n").split("\n");
}

/** LCS 行级差异(与 utils/diff.ts 同款), 返回与 base 对齐的操作序列。 */
function lcsOps(a: string[], b: string[]): RawOp[] {
  const n = a.length;
  const m = b.length;
  if (n === 0 && m === 0) return [];
  if (n === 0) return b.map((text) => ({ t: "add" as const, text }));
  if (m === 0) return a.map((text) => ({ t: "del" as const, text }));
  if (n * m > LCS_LIMIT) {
    return [
      ...a.map((text) => ({ t: "del" as const, text })),
      ...b.map((text) => ({ t: "add" as const, text })),
    ];
  }

  const dp: Int32Array[] = new Array(n + 1);
  dp[n] = new Int32Array(m + 1);
  for (let i = n - 1; i >= 0; i--) {
    const row = new Int32Array(m + 1);
    const next = dp[i + 1];
    const ai = a[i];
    for (let j = m - 1; j >= 0; j--) {
      if (ai === b[j]) row[j] = next[j + 1] + 1;
      else row[j] = next[j] > row[j + 1] ? next[j] : row[j + 1];
    }
    dp[i] = row;
  }

  const ops: RawOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ t: "ctx", text: a[i] });
      i++;
      j++;
    } else if (dp[i][j + 1] > dp[i + 1][j]) {
      ops.push({ t: "add", text: b[j] });
      j++;
    } else {
      ops.push({ t: "del", text: a[i] });
      i++;
    }
  }
  while (i < n) {
    ops.push({ t: "del", text: a[i] });
    i++;
  }
  while (j < m) {
    ops.push({ t: "add", text: b[j] });
    j++;
  }
  return ops;
}

interface SideDiff {
  /** 每个 base 行是否被该侧删除 */
  removed: boolean[];
  /** insertions[p] = 该侧在第 p 行之前插入的行(0 为文件首, base.length 为文件尾) */
  insertions: string[][];
}

/** 由对齐 base 的操作序列还原「每个 base 行上的变更」: 是否删除 + 插入位置。 */
function buildSide(base: string[], ops: RawOp[]): SideDiff {
  const removed = new Array<boolean>(base.length).fill(false);
  const insertions = Array.from({ length: base.length + 1 }, () => [] as string[]);
  let k = 0;
  let pending: string[] = [];
  for (const op of ops) {
    if (op.t === "add") {
      pending.push(op.text);
    } else {
      if (pending.length) {
        insertions[k].push(...pending);
        pending = [];
      }
      if (op.t === "del") removed[k] = true;
      k++;
    }
  }
  if (pending.length) insertions[k].push(...pending);
  return { removed, insertions };
}

/**
 * 变更区段(坐标空间):
 * - 偶数坐标 2k+2 → base 行 k
 * - 奇数坐标 2p+1 → 插入槽 p(第 p 行之前; p=0 为文件首, p=n 为文件尾)
 */
interface Region {
  lo: number;
  hi: number;
}

/** 找出变更区段: 被任一方删除的 base 行连成区段(含边界插入槽), 孤立插入为独立零宽区段。 */
function collectRegions(mine: SideDiff, theirs: SideDiff, n: number): Region[] {
  const regions: Region[] = [];

  // 1) 删除区段
  let s = -1;
  for (let k = 0; k <= n; k++) {
    const rem = k < n && (mine.removed[k] || theirs.removed[k]);
    if (rem && s < 0) s = k;
    if (!rem && s >= 0) {
      regions.push({ lo: 2 * s + 1, hi: 2 * (k - 1) + 3 });
      s = -1;
    }
  }

  // 2) 孤立插入槽(未被删除行吸收)
  for (let p = 0; p <= n; p++) {
    const hasIns = mine.insertions[p].length > 0 || theirs.insertions[p].length > 0;
    if (!hasIns) continue;
    const attached =
      (p > 0 && (mine.removed[p - 1] || theirs.removed[p - 1])) ||
      (p < n && (mine.removed[p] || theirs.removed[p]));
    if (!attached) regions.push({ lo: 2 * p + 1, hi: 2 * p + 1 });
  }

  regions.sort((a, b) => a.lo - b.lo);
  return regions;
}

/** 某侧在坐标区段内的内容。 */
function regionContent(side: SideDiff, base: string[], lo: number, hi: number): string[] {
  const lines: string[] = [];
  for (let c = lo; c <= hi; c++) {
    if (c % 2 === 0) {
      const k = (c - 2) / 2;
      if (k >= 0 && k < base.length && !side.removed[k]) lines.push(base[k]);
    } else {
      const p = (c - 1) / 2;
      if (p >= 0 && p <= base.length) lines.push(...(side.insertions[p] ?? []));
    }
  }
  return lines;
}

function same(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

const EMPTY_SIDE: SideDiff = { removed: [], insertions: [] };

function decideRegion(
  base: string[],
  mine: SideDiff,
  theirs: SideDiff,
  r: Region,
): MergeSegment {
  const m = regionContent(mine, base, r.lo, r.hi);
  const t = regionContent(theirs, base, r.lo, r.hi);
  if (same(m, t)) return { type: "context", lines: m };
  const b = regionContent(EMPTY_SIDE, base, r.lo, r.hi);
  if (same(m, b)) return { type: "theirs", lines: t, mine: m, theirs: t };
  if (same(t, b)) return { type: "mine", lines: m, mine: m, theirs: t };
  return { type: "conflict", lines: [], mine: m, theirs: t };
}

function emitContext(base: string[], from: number, to: number, out: MergeSegment[]): void {
  for (let c = from; c <= to; c++) {
    if (c % 2 !== 0) continue;
    const k = (c - 2) / 2;
    if (k >= 0 && k < base.length) out.push({ type: "context", lines: [base[k]] });
  }
}

function buildSegments(base: string[], mine: SideDiff, theirs: SideDiff): MergeSegment[] {
  const regions = collectRegions(mine, theirs, base.length);
  const segments: MergeSegment[] = [];
  let cursor = 0;
  for (const r of regions) {
    emitContext(base, cursor, r.lo - 1, segments);
    cursor = r.hi + 1;
    segments.push(decideRegion(base, mine, theirs, r));
  }
  emitContext(base, cursor, 2 * base.length, segments);
  return segments;
}

function renderCode(segments: MergeSegment[], hadTrailingNl: boolean): string {
  const parts: string[] = [];
  for (const s of segments) {
    if (s.type === "conflict") {
      parts.push("<<<<<<< 暂存(本地)");
      parts.push(...(s.mine ?? []));
      parts.push("=======");
      parts.push(...(s.theirs ?? []));
      parts.push(">>>>>>> 最新(远端)");
    } else {
      parts.push(...s.lines);
    }
  }
  let code = parts.join("\n");
  if (hadTrailingNl && code.length > 0 && !code.endsWith("\n")) code += "\n";
  return code;
}

/**
 * 三方行级合并。
 * - 任一侧未改动 base → 直接取另一侧(无冲突);
 * - 双方改动且结果一致 → 自动合并;
 * - 双方改动且结果不同 → 输出冲突标记, 并在 segments 中携带双方内容。
 */
export function merge3(base: string, mine: string, theirs: string): MergeResult {
  if (base === mine) return { code: theirs, segments: [], conflictCount: 0 };
  if (base === theirs) return { code: mine, segments: [], conflictCount: 0 };
  if (mine === theirs) return { code: mine, segments: [], conflictCount: 0 };

  const bl = splitLines(base);
  const hadTrailingNl =
    base.endsWith("\n") || mine.endsWith("\n") || theirs.endsWith("\n");

  const mSide = buildSide(bl, lcsOps(bl, splitLines(mine)));
  const tSide = buildSide(bl, lcsOps(bl, splitLines(theirs)));
  const segments = buildSegments(bl, mSide, tSide);
  const conflictCount = segments.filter((s) => s.type === "conflict").length;
  return { code: renderCode(segments, hadTrailingNl), segments, conflictCount };
}

/** 把一次冲突段按选择侧解析为代码内容。 */
export function resolveConflict(segments: MergeSegment[], pick: "mine" | "theirs" | "both"): string {
  const parts: string[] = [];
  for (const s of segments) {
    if (s.type !== "conflict") {
      parts.push(...s.lines);
      continue;
    }
    if (pick === "mine") parts.push(...(s.mine ?? []));
    else if (pick === "theirs") parts.push(...(s.theirs ?? []));
    else {
      parts.push(...(s.mine ?? []));
      parts.push(...(s.theirs ?? []));
    }
  }
  return parts.join("\n");
}

/** 降级路径: 找不到合并基点(草稿 head 提交缺失)时, 整文件作为一处冲突。 */
export function wholeFileConflict(mine: string, theirs: string): MergeResult {
  return {
    code: mine,
    segments: [
      { type: "conflict", lines: [], mine: splitLines(mine), theirs: splitLines(theirs) },
    ],
    conflictCount: 1,
  };
}