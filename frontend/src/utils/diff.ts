export type DiffLineType = "ctx" | "add" | "del";

export interface DiffLine {
  type: DiffLineType;
  /** 变更前(old)文件中的行号, 仅 ctx / del 行有 */
  oldNo?: number;
  /** 变更后(new)文件中的行号, 仅 ctx / add 行有 */
  newNo?: number;
  text: string;
}

export interface DiffHunk {
  /** 该 hunk 变更前文件起始行号(1 起) */
  oldStart: number;
  /** 该 hunk 变更后文件起始行号(1 起) */
  newStart: number;
  oldCount: number;
  newCount: number;
  lines: DiffLine[];
}

export interface DiffStats {
  add: number;
  del: number;
}

function splitLines(text: string): string[] {
  if (!text) return [];
  return text.replace(/\r\n/g, "\n").split("\n");
}

interface RawOp {
  type: DiffLineType;
  text: string;
}

interface InternalHunk {
  start: number;
  end: number;
  hunk: DiffHunk;
}

const LCS_LIMIT = 4_000_000;

/**
 * 基于 LCS 计算中间区段的编辑操作序列。
 * 超大差异(约 2000x2000 行)返回 null, 由调用方回退为整段替换。
 */
function lcsOps(a: string[], b: string[]): RawOp[] | null {
  const n = a.length;
  const m = b.length;
  if (n === 0 || m === 0) return null;
  if (n * m > LCS_LIMIT) return null;

  // dp[i][j] = a[i:] 与 b[j:] 的 LCS 长度
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
      ops.push({ type: "ctx", text: a[i] });
      i++;
      j++;
    } else if (dp[i][j + 1] > dp[i + 1][j]) {
      ops.push({ type: "add", text: b[j] });
      j++;
    } else {
      ops.push({ type: "del", text: a[i] });
      i++;
    }
  }
  while (i < n) {
    ops.push({ type: "del", text: a[i] });
    i++;
  }
  while (j < m) {
    ops.push({ type: "add", text: b[j] });
    j++;
  }
  return ops;
}

/** 由带行号的变更行序列推导 hunk 头信息。 */
function toHunk(lines: DiffLine[]): DiffHunk {
  let oldStart = 0;
  let newStart = 0;
  let oldCount = 0;
  let newCount = 0;
  for (const line of lines) {
    if (line.type !== "add") {
      oldCount++;
      if (oldStart === 0 && line.oldNo !== undefined) oldStart = line.oldNo - oldCount + 1;
    }
    if (line.type !== "del") {
      newCount++;
      if (newStart === 0 && line.newNo !== undefined) newStart = line.newNo - newCount + 1;
    }
  }
  return { oldStart, newStart, oldCount, newCount, lines };
}

/**
 * 计算两个文本的行级差异, 返回 git 风格的 hunk 列表。
 * 仅包含发生变更的位置及其附近 context 行, 并携带两侧行号。
 * @param context 每个变更块前后保留的上下文行数
 */
export function diffHunks(a: string, b: string, context = 3): DiffHunk[] {
  const la = splitLines(a);
  const lb = splitLines(b);
  if (a === b) return [];

  // 去掉公共前缀 / 公共后缀, 缩小 LCS 计算范围
  let pre = 0;
  const minLen = Math.min(la.length, lb.length);
  while (pre < minLen && la[pre] === lb[pre]) pre++;

  let suf = 0;
  while (
    suf < la.length - pre &&
    suf < lb.length - pre &&
    la[la.length - 1 - suf] === lb[lb.length - 1 - suf]
  ) {
    suf++;
  }

  const aMid = la.slice(pre, la.length - suf);
  const bMid = lb.slice(pre, lb.length - suf);

  let ops: RawOp[] | null;
  if (aMid.length === 0) {
    ops = bMid.map((text) => ({ type: "add" as const, text }));
  } else if (bMid.length === 0) {
    ops = aMid.map((text) => ({ type: "del" as const, text }));
  } else {
    ops = lcsOps(aMid, bMid);
  }
  if (ops === null) {
    ops = [
      ...aMid.map((text) => ({ type: "del" as const, text })),
      ...bMid.map((text) => ({ type: "add" as const, text })),
    ];
  }

  // 为每个操作行标注两侧行号
  let curOld = pre + 1;
  let curNew = pre + 1;
  const numbered: DiffLine[] = [];
  for (const op of ops) {
    if (op.type === "add") {
      numbered.push({ type: "add", newNo: curNew, text: op.text });
      curNew++;
    } else if (op.type === "del") {
      numbered.push({ type: "del", oldNo: curOld, text: op.text });
      curOld++;
    } else {
      numbered.push({ type: "ctx", oldNo: curOld, newNo: curNew, text: op.text });
      curOld++;
      curNew++;
    }
  }

  // 按变更块组装 hunk(前后各保留 context 行), 相邻/重叠的 hunk 合并
  const hunks: InternalHunk[] = [];
  let i = 0;
  while (i < numbered.length) {
    if (numbered[i].type === "ctx") {
      i++;
      continue;
    }
    let end = i;
    while (end < numbered.length && numbered[end].type !== "ctx") end++;

    let start = i;
    let back = 0;
    while (start > 0 && numbered[start - 1].type === "ctx" && back < context) {
      start--;
      back++;
    }
    let stop = end;
    let fwd = 0;
    while (stop < numbered.length && numbered[stop].type === "ctx" && fwd < context) {
      stop++;
      fwd++;
    }

    const last = hunks[hunks.length - 1];
    if (last && last.end >= start) {
      const merged = numbered.slice(last.start, stop);
      const h = toHunk(merged);
      last.hunk.oldStart = h.oldStart;
      last.hunk.newStart = h.newStart;
      last.hunk.oldCount = h.oldCount;
      last.hunk.newCount = h.newCount;
      last.hunk.lines = merged;
      last.end = stop;
    } else {
      hunks.push({ start, end: stop, hunk: toHunk(numbered.slice(start, stop)) });
    }
    i = stop;
  }

  return hunks.map((it) => it.hunk);
}

/** 从 hunk 列表汇总 +N -M 行数统计。 */
export function diffStatsFromHunks(hunks: DiffHunk[]): DiffStats {
  let add = 0;
  let del = 0;
  for (const h of hunks) {
    for (const l of h.lines) {
      if (l.type === "add") add++;
      else if (l.type === "del") del++;
    }
  }
  return { add, del };
}
