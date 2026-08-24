import * as monaco from "monaco-editor";
import libIndex from "./libApi.json";
import { formatCode } from "./utils/api";

// 基于本地类库索引的 code actions:
//   1. auto-import 快速修复: 诊断报"未定义 X"时, 若 X 命中 libApi/playwrightApi 的
//      模块成员, 提供"添加 import"修复。
//   2. organize imports: 调用后端 isort 排序/分组导入语句。
//   3. 代码格式化: pyright 不提供 formatting, 改由后端 /api/v1/format(black) 完成。

interface PyLibModuleMember {
  kind: "function" | "class" | "module";
  ret: string;
}

interface PyModule {
  members: Record<string, PyLibModuleMember>;
}

interface PyLibIndex {
  modules: Record<string, PyModule>;
  classes: Record<string, unknown>;
}

const LIB = libIndex as PyLibIndex;

// 模块名 -> 成员名 -> (kind, ret), 用于把"未定义名"映射到可导入的来源
const MEMBER_ORIGINS: Map<string, Array<{ member: string; module: string; kind: string }>> = new Map();
for (const [modPath, mod] of Object.entries(LIB.modules)) {
  for (const [member, info] of Object.entries(mod.members)) {
    if (info.kind === "module") continue;
    const list = MEMBER_ORIGINS.get(member) ?? [];
    list.push({ member, module: modPath, kind: info.kind });
    MEMBER_ORIGINS.set(member, list);
  }
}

function findImports(text: string): { endLine: number; hasModule: (m: string) => boolean } {
  const imported = new Set<string>();
  let lastImportLine = -1;
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*import\s+([a-zA-Z_][\w.]*)/);
    if (m) {
      imported.add(m[1].split(".")[0]);
      lastImportLine = Math.max(lastImportLine, 0);
      continue;
    }
    const f = line.match(/^\s*from\s+([a-zA-Z_][\w.]*)\s+import/);
    if (f) {
      imported.add(f[1].split(".")[0]);
      lastImportLine = Math.max(lastImportLine, 0);
    }
  }
  return {
    endLine: lastImportLine,
    hasModule: (m) => imported.has(m.split(".")[0]),
  };
}

function makeAutoImportAction(
  model: monaco.editor.ITextModel,
  name: string,
  origin: { member: string; module: string; kind: string },
): monaco.languages.CodeAction | null {
  const text = model.getValue();
  const { endLine, hasModule } = findImports(text);
  const fullModule = origin.module;
  const importLine = `from ${fullModule} import ${name}`;
  const already = hasModule(fullModule) || text.includes(importLine);
  if (already) return null;

  const insertLine = endLine >= 0 ? endLine + 1 : 0;
  const insertText = insertLine === 0 ? `${importLine}\n` : `\n${importLine}\n`;
  const pos = {
    startLineNumber: insertLine + 1,
    startColumn: 1,
    endLineNumber: insertLine + 1,
    endColumn: 1,
  };

  return {
    title: `添加 import: ${importLine}`,
    kind: "quickfix",
    isPreferred: false,
    diagnostics: [],
    edit: {
      edits: [
        {
          resource: model.uri,
          versionId: undefined,
          textEdit: { range: pos, text: insertText },
        },
      ],
    },
  };
}

/** 从诊断信息中提取"未定义的名字"。 */
interface MarkerLike {
  message: string;
}

function extractUndefinedNames(markers: MarkerLike[] | readonly MarkerLike[]): string[] {
  const names: string[] = [];
  for (const m of markers) {
    const q = m.message.match(/undefined name ['"]([A-Za-z_]\w*)['"]/);
    if (q) {
      names.push(q[1]);
      continue;
    }
    const nq = m.message.match(/Name ['"]([A-Za-z_]\w*)['"] is not defined/);
    if (nq) names.push(nq[1]);
    // pyright 风格: "foo" is not defined
    const p = m.message.match(/^["']([A-Za-z_]\w*)["'] is not defined/);
    if (p) names.push(p[1]);
  }
  return names;
}

/** 代码格式化: 调用后端 black, 供 Monaco formatDocument(按钮 / Shift+Alt+F)使用。 */
export function registerPythonFormatting(): void {
  monaco.languages.registerDocumentFormattingEditProvider("python", {
    provideDocumentFormattingEdits(model) {
      const code = model.getValue();
      return formatCode(code)
        .then((r) => {
          if (!r.ok || !r.formatted || r.formatted === code) return [];
          return [{ range: model.getFullModelRange(), text: r.formatted }];
        })
        .catch(() => []);
    },
  });
}

export function registerPythonCodeActions(): void {
  monaco.languages.registerCodeActionProvider("python", {
    provideCodeActions(model, _range, context) {
      const actions: monaco.languages.CodeAction[] = [];

      // auto-import
      for (const name of extractUndefinedNames(context.markers ?? [])) {
        const origins = MEMBER_ORIGINS.get(name);
        if (!origins) continue;
        const action = makeAutoImportAction(model, name, origins[0]);
        if (action) actions.push(action);
      }

      // organize imports
      actions.push({
        title: "整理导入 (isort)",
        kind: "source.organizeImports",
        command: {
          id: "python.organizeImports",
          title: "整理导入",
        },
      });

      return { actions, dispose: () => {} };
    },
  });
}
