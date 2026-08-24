import * as monaco from "monaco-editor";
import apiIndex from "./playwrightApi.json";
import libIndex from "./libApi.json";

// 补全/悬停/签名提示已由 pyright(LSP) 提供, 这里只保留变量语义高亮。

interface PyParam {
  name: string;
  kwonly: boolean;
  default: string | null;
  type: string;
}

interface PyMember {
  kind: "method" | "property";
  async: boolean;
  params: PyParam[];
  ret: string;
  doc: string;
}

interface PyClass {
  bases: string[];
  doc: string;
  init?: { params: PyParam[]; doc: string };
  members: Record<string, PyMember>;
}

interface PyApiIndex {
  version: string;
  classes: Record<string, PyClass>;
}

interface PyModuleMember {
  kind: "function" | "class" | "module";
  async: boolean;
  params: PyParam[];
  ret: string;
  doc: string;
}

interface PyModule {
  doc: string;
  members: Record<string, PyModuleMember>;
}

interface PyLibIndex {
  version: string;
  modules: Record<string, PyModule>;
  classes: Record<string, PyClass>;
}

const API = apiIndex as PyApiIndex;
const LIB = libIndex as PyLibIndex;
const CLASSES: Record<string, PyClass> = { ...API.classes, ...LIB.classes };
const CLASS_NAMES = new Set(Object.keys(CLASSES));
const MODULES = LIB.modules;
const MODULE_NAMES = new Set(Object.keys(MODULES));

const BUILTIN_TYPES = new Set([
  "str",
  "int",
  "float",
  "complex",
  "bool",
  "bytes",
  "bytearray",
  "memoryview",
  "list",
  "dict",
  "set",
  "frozenset",
  "tuple",
  "range",
  "slice",
  "object",
  "type",
  "None",
]);

interface ResolvedType {
  cls: string | null;
  module: string | null;
  array: boolean;
  fn?: { module: string; member: PyModuleMember } | null;
}

interface PyToken {
  type: "name" | "number" | "string" | "op" | "newline";
  value: string;
  start: number;
  end: number;
}

const DEFAULT_GLOBALS: Array<[string, string]> = [
  ["page", "Page"],
  ["context", "BrowserContext"],
  ["browser", "Browser"],
  ["save_page", "object"],
  ["save_content", "object"],
  ["get_login_ticket", "object"],
  ["set_login_ticket", "object"],
  ["page_login", "object"],
  ["capture_login_state", "object"],
  ["restore_login_state", "object"],
];

// ---------------------------------------------------------------- tokenizer

function tokenize(code: string): PyToken[] {
  const tokens: PyToken[] = [];
  const n = code.length;
  let i = 0;
  while (i < n) {
    const c = code[i];
    if (c === "\n") {
      tokens.push({ type: "newline", value: "\n", start: i, end: ++i });
      continue;
    }
    if (c === " " || c === "\t" || c === "\r") {
      i++;
      continue;
    }
    if (c === "#") {
      while (i < n && code[i] !== "\n") i++;
      continue;
    }
    if (c === '"' || c === "'") {
      const start = i;
      const quote = c;
      const triple = code.startsWith(quote.repeat(3), i);
      const qLen = triple ? 3 : 1;
      let j = i + qLen;
      while (j < n) {
        if (code[j] === "\\") {
          j += 2;
          continue;
        }
        if (code.startsWith(quote.repeat(qLen), j)) {
          j += qLen;
          break;
        }
        if (!triple && code[j] === "\n") break;
        j++;
      }
      tokens.push({ type: "string", value: code.slice(start, j), start, end: j });
      i = j;
      continue;
    }
    if (/[0-9]/.test(c)) {
      const start = i;
      while (i < n && /[0-9a-zA-Z_.]/.test(code[i])) i++;
      tokens.push({ type: "number", value: code.slice(start, i), start, end: i });
      continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      const start = i;
      while (i < n && /[A-Za-z0-9_]/.test(code[i])) i++;
      tokens.push({ type: "name", value: code.slice(start, i), start, end: i });
      continue;
    }
    const three = code.slice(i, i + 3);
    const two = code.slice(i, i + 2);
    const ops3 = ["**=", "//=", "<<=", ">>=", "..."];
    const ops2 = ["**", "//", "<<", ">>", "<=", ">=", "==", "!=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "->", ":="];
    let matched: string;
    if (ops3.includes(three)) matched = three;
    else if (ops2.includes(two)) matched = two;
    else matched = c;
    tokens.push({ type: "op", value: matched, start: i, end: i + matched.length });
    i += matched.length;
  }
  return tokens;
}

function findMatching(tokens: PyToken[], openIdx: number, end: number, open: string, close: string): number {
  let depth = 0;
  for (let i = openIdx; i < end; i++) {
    const t = tokens[i];
    if (t.type === "newline") continue;
    if (t.value === open) depth++;
    else if (t.value === close) {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

// ------------------------------------------------------------------ types

function primaryTypeOf(ret: string): ResolvedType | null {
  const s = ret.replace(/\s+/g, "");
  if (!s || s === "None") return { cls: null, module: null, array: false };
  const quoted = s.match(/['"]([A-Za-z_][A-Za-z0-9_]*)['"]/);
  const arrayish = /List|Iterable|Generator|Set\b|Sequence|Tuple/.test(s);
  if (quoted) return { cls: quoted[1], module: null, array: arrayish };
  const inner = s
    .replace(/^typing\./, "")
    .replace(/^(Optional|List|Iterable|Generator|Set\b|Sequence|Tuple|Union|Dict)\[/, "")
    .replace(/\]$/, "");
  if (CLASS_NAMES.has(inner)) return { cls: inner, module: null, array: arrayish };
  const simple = inner.slice(inner.lastIndexOf(".") + 1);
  if (CLASS_NAMES.has(simple)) return { cls: simple, module: null, array: arrayish };
  if (BUILTIN_TYPES.has(inner)) return { cls: inner, module: null, array: arrayish };
  return { cls: null, module: null, array: arrayish };
}

function primaryTypeOfInModule(ret: string, module: string): ResolvedType | null {
  const s = ret.replace(/\s+/g, "");
  if (!s || s === "None") return { cls: null, module: null, array: false };
  const arrayish = /List|Iterable|Generator|Set\b|Sequence|Tuple/.test(s);
  const quoted = s.match(/['"]([A-Za-z_][A-Za-z0-9_.]*)['"]/);
  if (quoted) {
    const key = `${module}.${quoted[1]}`;
    if (CLASS_NAMES.has(key)) return { cls: key, module: null, array: arrayish };
    return { cls: quoted[1], module: null, array: arrayish };
  }
  const inner = s
    .replace(/^typing\./, "")
    .replace(/^(Optional|List|Iterable|Generator|Set\b|Sequence|Tuple|Union|Dict)\[/, "")
    .replace(/\]$/, "");
  const key = `${module}.${inner}`;
  if (CLASS_NAMES.has(key)) return { cls: key, module: null, array: arrayish };
  if (CLASS_NAMES.has(inner)) return { cls: inner, module: null, array: arrayish };
  const simple = inner.slice(inner.lastIndexOf(".") + 1);
  if (CLASS_NAMES.has(simple)) return { cls: simple, module: null, array: arrayish };
  if (BUILTIN_TYPES.has(inner)) return { cls: inner, module: null, array: arrayish };
  return { cls: null, module: null, array: arrayish };
}

function makeResolver(symbols: Map<string, ResolvedType | null>) {
  return (name: string): ResolvedType | null => {
    if (symbols.has(name)) return symbols.get(name) ?? null;
    if (CLASS_NAMES.has(name)) return { cls: name, module: null, array: false };
    if (MODULE_NAMES.has(name)) return { cls: null, module: name, array: false };
    return null;
  };
}

function parseExpr(
  tokens: PyToken[],
  start: number,
  end: number,
  resolveName: (n: string) => ResolvedType | null,
): ResolvedType | null {
  let i = start;
  while (i < end && tokens[i].type === "newline") i++;
  if (i >= end) return null;
  if (tokens[i].type === "name" && tokens[i].value === "await") {
    i++;
    while (i < end && tokens[i].type === "newline") i++;
  }
  let res: ResolvedType | null = null;
  const t = tokens[i];
  if (!t) return null;
  if (t.type === "name") {
    res = resolveName(t.value);
    i++;
  } else if (t.type === "number" || t.type === "string") {
    res = { cls: null, module: null, array: false };
    i++;
  } else if (t.value === "(") {
    const close = findMatching(tokens, i, end, "(", ")");
    if (close === -1) return null;
    res = parseExpr(tokens, i + 1, close, resolveName);
    i = close + 1;
  } else if (t.value === "[") {
    const close = findMatching(tokens, i, end, "[", "]");
    i = close === -1 ? end : close + 1;
    res = null;
  } else {
    return null;
  }
  while (i < end) {
    const op = tokens[i];
    if (op.type !== "op") break;
    if (op.value === ".") {
      const nameTok = tokens[i + 1];
      if (!nameTok || nameTok.type !== "name") break;
      if (res?.module) {
        const member = MODULES[res.module]?.members[nameTok.value];
        if (member?.kind === "function") res = primaryTypeOfInModule(member.ret, res.module);
        else if (member?.kind === "class") res = { cls: member.ret, module: null, array: false };
        else if (member?.kind === "module") res = { cls: null, module: member.ret, array: false };
        else res = null;
      } else {
        const info = res?.cls ? CLASSES[res.cls]?.members[nameTok.value] ?? null : null;
        if (info) {
          const dot = res?.cls?.lastIndexOf(".");
          const mod = dot !== undefined && dot !== -1 && res?.cls ? res.cls.slice(0, dot) : null;
          res = mod ? primaryTypeOfInModule(info.ret, mod) : primaryTypeOf(info.ret);
        } else {
          res = null;
        }
      }
      i += 2;
    } else if (op.value === "(") {
      const close = findMatching(tokens, i, end, "(", ")");
      i = close === -1 ? end : close + 1;
    } else if (op.value === "[") {
      const close = findMatching(tokens, i, end, "[", "]");
      i = close === -1 ? end : close + 1;
      if (res) res = res.array ? { cls: res.cls, module: res.module, array: false } : null;
    } else {
      break;
    }
  }
  return res;
}

// -------------------------------------------------------------- statements

function splitStatements(tokens: PyToken[]): PyToken[][] {
  const stmts: PyToken[][] = [];
  let cur: PyToken[] = [];
  let depth = 0;
  for (const t of tokens) {
    if (t.type === "newline" && depth === 0) {
      if (cur.length) {
        stmts.push(cur);
        cur = [];
      }
      continue;
    }
    cur.push(t);
    if (t.value === "(" || t.value === "[" || t.value === "{") depth++;
    else if (t.value === ")" || t.value === "]" || t.value === "}") depth--;
  }
  if (cur.length) stmts.push(cur);
  return stmts;
}

function findTopLevel(tokens: PyToken[], value: string, type?: PyToken["type"]): number {
  let depth = 0;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.value === "(" || t.value === "[" || t.value === "{") depth++;
    else if (t.value === ")" || t.value === "]" || t.value === "}") depth--;
    else if (depth === 0 && t.value === value && (!type || t.type === type)) return i;
  }
  return -1;
}

function findTopLevelEq(tokens: PyToken[]): number {
  let depth = 0;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.value === "(" || t.value === "[" || t.value === "{") depth++;
    else if (t.value === ")" || t.value === "]" || t.value === "}") depth--;
    else if (depth === 0 && t.value === "=") {
      const prev = tokens[i - 1];
      const bad = prev && ["=", "!", "<", ">", "+", "-", "*", "/", "%", "&", "|", "^", ":", ";", ","].includes(prev.value);
      if (!bad) return i;
    }
  }
  return -1;
}

function analyzeStatement(stmt: PyToken[], symbols: Map<string, ResolvedType | null>): void {
  const s = stmt.filter((t) => t.type !== "newline");
  if (s.length === 0) return;
  let idx = 0;
  if (s[0].type === "name" && s[0].value === "async") idx = 1;
  const first = s[idx];
  if (!first || first.type !== "name") return;
  const kw = first.value;
  const resolver = makeResolver(symbols);

  if (kw === "import") {
    const asIdx = findTopLevel(s, "as", "name");
    const aliasTok = asIdx !== -1 ? s[asIdx + 1] : null;
    const nameEnd = asIdx !== -1 ? asIdx : s.length;
    let name = "";
    let i = idx + 1;
    while (i < nameEnd) {
      const t = s[i];
      if (t.type === "name") {
        name += (name ? "." : "") + t.value;
        i++;
        continue;
      }
      if (t.type === "op" && t.value === ".") {
        i++;
        continue;
      }
      break;
    }
    const root = name.split(".")[0];
    const alias = aliasTok && aliasTok.type === "name" ? aliasTok.value : null;
    const sym = alias ?? root;
    if (alias && MODULE_NAMES.has(name)) {
      symbols.set(sym, { cls: null, module: name, array: false });
    } else if (MODULE_NAMES.has(root)) {
      symbols.set(sym, { cls: null, module: root, array: false });
    } else if (MODULE_NAMES.has(name)) {
      symbols.set(sym, { cls: null, module: name, array: false });
    } else if (CLASS_NAMES.has(name)) {
      symbols.set(sym, { cls: name, module: null, array: false });
    }
    return;
  }

  if (kw === "from") {
    const importIdx = s.findIndex((t) => t.type === "name" && t.value === "import");
    if (importIdx !== -1) {
      let modPath = "";
      for (let i = idx + 1; i < importIdx; i++) {
        const t = s[i];
        if (t.type === "name") modPath += (modPath ? "." : "") + t.value;
      }
      const mod = MODULES[modPath];
      const register = (name: string, alias: string) => {
        if (mod) {
          const member = mod.members[name];
          if (member?.kind === "class") {
            symbols.set(alias, { cls: member.ret, module: null, array: false });
            return;
          }
          if (member?.kind === "module") {
            symbols.set(alias, { cls: null, module: member.ret, array: false });
            return;
          }
          if (member?.kind === "function") {
            symbols.set(alias, { cls: primaryTypeOfInModule(member.ret, modPath)?.cls ?? null, module: null, array: false, fn: { module: modPath, member } });
            return;
          }
        }
        if (CLASS_NAMES.has(name)) symbols.set(alias, { cls: name, module: null, array: false });
      };
      let current: string | null = null;
      for (let i = importIdx + 1; i < s.length; i++) {
        const t = s[i];
        if (t.type === "name") {
          if (t.value === "as" && current) {
            const alias = s[i + 1];
            if (alias && alias.type === "name") {
              register(current, alias.value);
              current = null;
              i += 1;
            }
          } else {
            if (current) register(current, current);
            current = t.value;
          }
        } else if (t.value === ",") {
          if (current) register(current, current);
          current = null;
        } else if (t.value === "*") {
          return;
        }
      }
      if (current) register(current, current);
    }
    return;
  }

  if (kw === "for") {
    const inIdx = s.findIndex((t) => t.type === "name" && t.value === "in");
    if (inIdx !== -1) {
      const varTok = s[idx + 1];
      const res = parseExpr(s, inIdx + 1, s.length, resolver);
      if (varTok && varTok.type === "name") {
        if (res?.cls) {
          symbols.set(varTok.value, { cls: res.cls, module: null, array: false });
        } else {
          symbols.set(varTok.value, null);
        }
      }
    }
    return;
  }

  if (kw === "with") {
    const asIdx = s.findIndex((t) => t.type === "name" && t.value === "as");
    if (asIdx !== -1) {
      const varTok = s[asIdx + 1];
      const res = parseExpr(s, idx + 1, asIdx, resolver);
      if (varTok && varTok.type === "name") symbols.set(varTok.value, res);
    }
    return;
  }

  if (kw === "def" || kw === "class" || kw === "del") {
    if (kw === "del") {
      const v = s[idx + 1];
      if (v && v.type === "name") symbols.delete(v.value);
    }
    return;
  }

  const eqIdx = findTopLevelEq(s);
  if (eqIdx !== -1) {
    const lhs = s.slice(0, eqIdx);
    const rhs = s.slice(eqIdx + 1);
    if (lhs.some((t) => t.value === ",")) return;
    let names: string[] = [];
    let annotation: ResolvedType | null = null;
    const colonIdx = lhs.findIndex((t) => t.value === ":");
    if (colonIdx !== -1) {
      const nameTok = lhs[0];
      if (nameTok?.type === "name") names = [nameTok.value];
      annotation = primaryTypeOf(lhs.slice(colonIdx + 1).map((t) => t.value).join(""));
    } else {
      const nameTok = lhs[0];
      if (nameTok?.type === "name") names = [nameTok.value];
    }
    const res = annotation ?? parseExpr(rhs, 0, rhs.length, resolver);
    for (const n of names) symbols.set(n, res);
    return;
  }

  const colonIdx = findTopLevel(s, ":");
  if (colonIdx === 1) {
    const nameTok = s[0];
    if (nameTok?.type === "name") {
      symbols.set(nameTok.value, primaryTypeOf(s.slice(2).map((t) => t.value).join("")));
    }
  }
}

function buildSymbols(text: string): Map<string, ResolvedType | null> {
  const symbols = new Map<string, ResolvedType | null>();
  for (const [n, cls] of DEFAULT_GLOBALS) symbols.set(n, { cls, module: null, array: false });
  const tokens = tokenize(text);
  for (const stmt of splitStatements(tokens)) analyzeStatement(stmt, symbols);
  return symbols;
}

// ------------------------------------------------------------- inlay hints

const PY_KEYWORDS = new Set([
  "False", "None", "True", "and", "as", "assert", "async", "await", "break",
  "case", "class", "continue", "def", "del", "elif", "else", "except",
  "finally", "for", "from", "global", "if", "import", "in", "is", "lambda",
  "match", "nonlocal", "not", "or", "pass", "raise", "return", "try",
  "while", "with", "yield",
]);

function lineStarts(text: string): number[] {
  const starts: number[] = [0];
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) === 10) starts.push(i + 1);
  }
  return starts;
}

function locate(offset: number, starts: number[]): { line: number; col: number } {
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= offset) lo = mid;
    else hi = mid - 1;
  }
  return { line: lo, col: offset - starts[lo] };
}

function findExprStart(tokens: PyToken[], beforeIdx: number): number {
  let i = beforeIdx - 1;
  while (i >= 0 && tokens[i].type === "newline") i--;
  let start = i;
  let depth = 0;
  while (i >= 0) {
    const t = tokens[i];
    if (t.value === ")" || t.value === "]" || t.value === "}") {
      depth++;
      start = i;
      i--;
      continue;
    }
    if (t.value === "(" || t.value === "[" || t.value === "{") {
      depth--;
      start = i;
      i--;
      continue;
    }
    if (depth > 0) {
      start = i;
      i--;
      continue;
    }
    if (t.type === "name" || t.type === "number" || t.type === "string") {
      start = i;
      i--;
      continue;
    }
    if (t.type === "op" && t.value === ".") {
      start = i;
      i--;
      continue;
    }
    break;
  }
  return start;
}

/** 解析调用点 `expr.name(` 中被调用成员的参数列表; 返回 null 表示无法解析。 */
function resolveCalleeParams(
  tokens: PyToken[],
  symbols: Map<string, ResolvedType | null>,
  nameIdx: number,
): PyParam[] | null {
  const name = tokens[nameIdx].value;
  if (PY_KEYWORDS.has(name)) return null;
  // 方法调用: `<receiver> . <name> (`
  let dotIdx = nameIdx - 1;
  while (dotIdx >= 0 && tokens[dotIdx].type === "newline") dotIdx--;
  if (dotIdx >= 1 && tokens[dotIdx].type === "op" && tokens[dotIdx].value === ".") {
    const resolver = makeResolver(symbols);
    const recv = parseExpr(tokens, findExprStart(tokens, dotIdx), dotIdx, resolver);
    if (recv?.cls) {
      const member = CLASSES[recv.cls]?.members[name];
      if (member?.kind === "method") return member.params.filter((p) => p.name !== "self");
    }
    if (recv?.module) {
      const member = MODULES[recv.module]?.members[name];
      if (member?.kind === "function") return member.params;
    }
    return null;
  }
  // 普通调用: `name(` 或 `from x import name` 的别名
  const sym = symbols.get(name);
  if (sym?.fn) {
    const member = sym.fn.member;
    if (member?.kind === "function") return member.params;
  }
  if (CLASS_NAMES.has(name)) {
    return CLASSES[name]?.init?.params ?? null;
  }
  return null;
}

function splitCallArgs(tokens: PyToken[], start: number, end: number): PyToken[][] {
  const args: PyToken[][] = [];
  let cur: PyToken[] = [];
  let depth = 0;
  for (let i = start; i < end; i++) {
    const t = tokens[i];
    if (t.type === "newline") continue;
    if (t.value === "(" || t.value === "[" || t.value === "{") {
      depth++;
      cur.push(t);
      continue;
    }
    if (t.value === ")" || t.value === "]" || t.value === "}") {
      depth--;
      cur.push(t);
      continue;
    }
    if (t.value === "," && depth === 0) {
      if (cur.length) args.push(cur);
      cur = [];
      continue;
    }
    cur.push(t);
  }
  if (cur.length) args.push(cur);
  return args;
}

function argIsKeyword(arg: PyToken[]): boolean {
  return (
    arg.length >= 2 &&
    arg[0].type === "name" &&
    arg[1].type === "op" &&
    arg[1].value === "="
  );
}

/** 计算 Pylance 风格"参数名提示" inlay hints(仅位置实参)。 */
function providePythonInlayHints(model: monaco.editor.ITextModel): monaco.languages.InlayHintList {
  const text = model.getValue();
  const tokens = tokenize(text);
  const symbols = buildSymbols(text);
  const starts = lineStarts(text);
  const hints: monaco.languages.InlayHint[] = [];

  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.type !== "name") continue;
    let j = i + 1;
    while (j < tokens.length && tokens[j].type === "newline") j++;
    if (j >= tokens.length || tokens[j].value !== "(") continue;

    const params = resolveCalleeParams(tokens, symbols, i);
    if (!params || params.length === 0) continue;

    const close = findMatching(tokens, j, tokens.length, "(", ")");
    if (close === -1) continue;

    const args = splitCallArgs(tokens, j + 1, close);
    let paramIdx = 0;
    for (const arg of args) {
      if (argIsKeyword(arg)) continue;
      if (arg[0].value === "*") continue;
      const param = params[paramIdx];
      if (!param) break;
      paramIdx++;
      const p = locate(arg[0].start, starts);
      hints.push({
        position: { lineNumber: p.line + 1, column: p.col + 1 },
        label: `${param.name}: `,
        kind: monaco.languages.InlayHintKind.Parameter,
        paddingRight: false,
        paddingLeft: false,
      });
    }
  }
  return { hints, dispose: () => {} };
}

// -------------------------------------------------------- semantic tokens

function collectParameterNames(tokens: PyToken[]): Set<string> {
  const params = new Set<string>();
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.type !== "name" || t.value !== "def") continue;
    let j = i + 1;
    while (j < tokens.length && tokens[j].type === "newline") j++;
    const close = j < tokens.length ? findMatching(tokens, j + 1, tokens.length, "(", ")") : -1;
    if (close === -1) continue;
    for (let k = j; k < close; k++) {
      const tk = tokens[k];
      if (tk.type === "name") params.add(tk.value);
    }
  }
  return params;
}

function encodeVariableSemanticTokens(text: string): Uint32Array {
  const symbols = buildSymbols(text);
  const tokens = tokenize(text);
  const parameters = collectParameterNames(tokens);
  const starts: number[] = [0];
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) starts.push(i + 1);
  const locate = (offset: number): { line: number; col: number } => {
    let lo = 0;
    let hi = starts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (starts[mid] <= offset) lo = mid;
      else hi = mid - 1;
    }
    return { line: lo, col: offset - starts[lo] };
  };
  const data: number[] = [];
  let prevLine = 0;
  let prevCol = 0;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.type !== "name") continue;
    const sym = symbols.get(t.value);
    if (!sym || sym.module) continue;
    let j = i + 1;
    while (j < tokens.length && tokens[j].type === "newline") j++;
    if (j < tokens.length && tokens[j].value === "(") continue;
    // 参数使用单独的 token 类型(更贴近 Pylance 的参数着色)
    const tokenType = parameters.has(t.value) ? 1 : 0;
    const p = locate(t.start);
    const deltaLine = p.line - prevLine;
    data.push(deltaLine, deltaLine === 0 ? p.col - prevCol : p.col, t.end - t.start, tokenType, 0);
    prevLine = p.line;
    prevCol = p.col;
  }
  return Uint32Array.from(data);
}

let registered = false;

export function registerPythonIntelliSense(): void {
  if (registered) return;
  registered = true;

  monaco.languages.registerDocumentSemanticTokensProvider("python", {
    getLegend: () => ({ tokenTypes: ["variable", "parameter"], tokenModifiers: [] }),
    provideDocumentSemanticTokens(model) {
      return { data: encodeVariableSemanticTokens(model.getValue()) };
    },
    releaseDocumentSemanticTokens: () => {},
  });

  monaco.languages.registerInlayHintsProvider("python", {
    provideInlayHints(model) {
      return providePythonInlayHints(model);
    },
  });
}
