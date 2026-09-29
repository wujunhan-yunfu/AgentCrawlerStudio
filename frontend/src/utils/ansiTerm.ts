/**
 * 终端语义归一器: 把流式 stdout/stderr 原始字节(含 \r 行覆盖、ANSI 控制序列)
 * 解析为「已提交行 + 光标所在活动行」, 供输出面板做原位覆盖渲染。
 *
 * 判定依据是字节语义而非内容: \r 前缀 = 行首重绘(覆盖活动行), 无 \r 文本 = 追加续写,
 * \n = 提交活动行。因此 tqdm 的 \r 帧与 print("x", end="") 的纯文本天然可区分。
 *
 * 支持增量输入(SSE 帧可任意切分): 半截 ANSI 序列保留在内部 buf, 跨帧状态不变。
 */

export interface TermState {
  /** 未解析完的尾部字节(含半截 ANSI 序列) */
  buf: string;
  /** 全部行(含被光标移走、仅剩历史值的行) */
  rows: string[];
  /** 光标所在行下标, 0 <= cursor <= rows.length(等于 rows.length 表示位于末尾待追加行) */
  cursor: number;
}

export interface TermView {
  /** 已提交的完整行(不含光标所在行) */
  committed: string[];
  /** 光标所在活动行, 无则 null */
  live: string | null;
}

export function createTermState(): TermState {
  return { buf: "", rows: [], cursor: 0 };
}

/** 当前渲染视图: 除光标行外的全部行视为已提交, 光标行作为活动行实时替换。 */
export function termView(state: TermState): TermView {
  const { rows, cursor } = state;
  const live = cursor < rows.length ? rows[cursor] : null;
  const committed: string[] = [];
  for (let i = 0; i < rows.length; i++) {
    if (i !== cursor) committed.push(rows[i]);
  }
  return { committed, live };
}

function isFinalByte(code: number): boolean {
  return code >= 0x40 && code <= 0x7e;
}

/** 自 start 起下一个控制字符(\n / \r / \x1b)的下标, 无则 -1。 */
function nextControl(buf: string, start: number): number {
  for (let i = start; i < buf.length; i++) {
    const c = buf.charCodeAt(i);
    if (c === 0x0a || c === 0x0d || c === 0x1b) return i;
  }
  return -1;
}

/** 递增喂入原始字节片段, 返回新的 TermState(不改动入参)。 */
export function pushTerm(state: TermState, chunk: string): TermState {
  const buf = state.buf + chunk;
  const rows = state.rows.slice();
  let cursor = state.cursor;
  let i = 0;

  const rowText = (): string => (cursor < rows.length ? rows[cursor] : "");
  const setRow = (text: string): void => {
    if (cursor < rows.length) rows[cursor] = text;
    else rows.push(text);
  };
  const appendRow = (text: string): void => setRow(rowText() + text);

  while (i < buf.length) {
    const n = nextControl(buf, i);
    if (n === -1) {
      appendRow(buf.slice(i));
      i = buf.length;
      break;
    }
    if (n > i) {
      appendRow(buf.slice(i, n));
      i = n;
    }
    const c = buf.charCodeAt(i);
    if (c === 0x0a) {
      cursor = Math.min(rows.length, cursor + 1);
      i += 1;
    } else if (c === 0x0d) {
      if (i + 1 < buf.length && buf.charCodeAt(i + 1) === 0x0a) {
        cursor = Math.min(rows.length, cursor + 1);
        i += 2;
      } else {
        setRow("");
        i += 1;
      }
    } else {
      if (i + 1 >= buf.length) {
        i += 1;
        break;
      }
      const next = buf.charCodeAt(i + 1);
      if (next === 0x5b) {
        let j = i + 2;
        while (j < buf.length && !isFinalByte(buf.charCodeAt(j))) j += 1;
        if (j >= buf.length) {
          i += 1;
          break;
        }
        const seq = buf.slice(i, j + 1);
        const final = buf.charAt(j);
        if (final === "A") cursor = Math.max(0, cursor - 1);
        else if (final === "B") cursor = Math.min(rows.length, cursor + 1);
        else if (final === "K" && seq.includes("2")) setRow("");
        i = j + 1;
      } else if (next === 0x5d) {
        let j = i + 2;
        let terminated = false;
        while (j < buf.length) {
          const code = buf.charCodeAt(j);
          if (code === 0x07) {
            j += 1;
            terminated = true;
            break;
          }
          if (code === 0x1b && j + 1 < buf.length && buf.charCodeAt(j + 1) === 0x5c) {
            j += 2;
            terminated = true;
            break;
          }
          j += 1;
        }
        if (!terminated) {
          i += 1;
          break;
        }
        i = j;
      } else {
        i += 2;
      }
    }
  }

  return { buf: buf.slice(i), rows, cursor };
}

/** 运行结束/停止时冲刷: 把光标所在活动行转为已提交行(光标移到末尾)。 */
export function flushTerm(state: TermState): TermState {
  return { buf: "", rows: state.rows, cursor: state.rows.length };
}

/** 整段文本的离线归一(供 Agent tool_result 等一次性内容复用): 归一并保留全部行。 */
export function normalizeTerm(text: string): string {
  let state = createTermState();
  state = pushTerm(state, text);
  state = flushTerm(state);
  return state.rows.join("\n");
}