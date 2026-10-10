import { useEffect, useRef, useState } from "react";
import Editor, { type OnMount } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import { ensureMonacoServices, PYTHON_THEME, PYTHON_THEME_LIGHT } from "../monaco";
import { ensureLspInfo, startPythonLsp } from "../pythonLsp";
import { organizeImports } from "../utils/api";
import { useTheme } from "../theme";
import { useI18n } from "../i18n";

interface Props {
  value: string;
  onChange: (value: string) => void;
  onRun: () => void;
  onFormatError?: (message: string) => void;
  onEditorReady?: (editor: monaco.editor.IStandaloneCodeEditor) => void;
}

class PlaceholderWidget implements monaco.editor.IContentWidget {
  private readonly domNode: HTMLDivElement;

  constructor(text: string) {
    const el = document.createElement("div");
    el.className = "monaco-placeholder";
    el.textContent = text;
    el.style.pointerEvents = "none";
    this.domNode = el;
  }

  getId(): string {
    return "code-editor-placeholder";
  }

  getDomNode(): HTMLElement {
    return this.domNode;
  }

  getPosition(): monaco.editor.IContentWidgetPosition | null {
    return {
      position: { lineNumber: 1, column: 1 },
      preference: [monaco.editor.ContentWidgetPositionPreference.EXACT],
    };
  }
}

function syncPlaceholder(
  editor: monaco.editor.IStandaloneCodeEditor,
  widget: monaco.editor.IContentWidget,
): void {
  const model = editor.getModel();
  if (!model) return;
  if (model.getValue().trim() === "") {
    editor.addContentWidget(widget);
  } else {
    editor.removeContentWidget(widget);
  }
}

function applyOrganizeImports(editor: monaco.editor.IStandaloneCodeEditor): Promise<boolean> {
  const model = editor.getModel();
  if (!model) return Promise.resolve(false);
  const code = model.getValue();
  return organizeImports(code)
    .then((r) => {
      if (!r.ok || !r.formatted || r.formatted === code) return false;
      model.pushEditOperations(
        [],
        [{ range: model.getFullModelRange(), text: r.formatted }],
        () => null,
      );
      return true;
    })
    .catch(() => false);
}

export default function CodeEditor({ value, onChange, onRun, onFormatError, onEditorReady }: Props) {
  const onFormatErrorRef = useRef(onFormatError);
  onFormatErrorRef.current = onFormatError;
  const { resolved } = useTheme();
  const { t } = useI18n();
  const placeholderText = t("editor.placeholder");

  const [ready, setReady] = useState(false);
  const [docUri, setDocUri] = useState<string | null>(null);
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const placeholderRef = useRef<PlaceholderWidget | null>(null);
  const contentSubRef = useRef<monaco.IDisposable | null>(null);

  useEffect(() => {
    let cancelled = false;
    ensureMonacoServices()
      .then(() => ensureLspInfo())
      .then((uri) => {
        if (cancelled) return;
        setDocUri(uri);
        setReady(true);
      })
      .catch((e) => {
        console.error("LSP 初始化失败", e);
        setReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    return () => {
      contentSubRef.current?.dispose();
      contentSubRef.current = null;
      const editor = editorRef.current;
      const widget = placeholderRef.current;
      if (editor && widget) {
        editor.removeContentWidget(widget);
      }
      placeholderRef.current = null;
      editorRef.current = null;
    };
  }, []);

  // 外部 value 变化(Agent 回写 / 格式化等)才主动写回模型;
  // 用户键盘输入产生的 value 变化与模型内容一致, 这里会被 guard 跳过, 避免 @monaco-editor/react
  // 内部对受控 value 做全量 executeEdits(forceMoveMarkers) 导致光标跳到末尾。
  useEffect(() => {
    const editor = editorRef.current;
    const model = editor?.getModel();
    if (!editor || !model) return;
    if (value === model.getValue()) return;
    const prevPosition = editor.getPosition();
    model.pushEditOperations(
      [],
      [{ range: model.getFullModelRange(), text: value }],
      () => null,
    );
    if (prevPosition) editor.setPosition(prevPosition);
  }, [value]);

  const handleMount: OnMount = (editor) => {
    editorRef.current = editor;
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => onRun());
    editor.addCommand(monaco.KeyMod.Shift | monaco.KeyMod.Alt | monaco.KeyCode.KeyF, () =>
      editor.getAction("editor.action.formatDocument")?.run(),
    );
    editor.addCommand(monaco.KeyMod.Shift | monaco.KeyMod.Alt | monaco.KeyCode.KeyO, () =>
      void applyOrganizeImports(editor),
    );
    monaco.editor.registerCommand?.("python.organizeImports", () =>
      void applyOrganizeImports(editor),
    );
    const widget = new PlaceholderWidget(placeholderText);
    placeholderRef.current = widget;
    syncPlaceholder(editor, widget);
    const model = editor.getModel();
    contentSubRef.current =
      model?.onDidChangeContent(() => syncPlaceholder(editor, widget)) ?? null;
    // 编辑器与模型创建完成后启动语言客户端, 确保 didOpen 能拿到文档
    startPythonLsp();
    onEditorReady?.(editor);
  };

  useEffect(() => {
    const node = placeholderRef.current?.getDomNode();
    if (node) node.textContent = placeholderText;
  }, [placeholderText]);

  if (!ready) return null;

  return (
    <Editor
      height="100%"
      language="python"
      theme={resolved === "light" ? PYTHON_THEME_LIGHT : PYTHON_THEME}
      path={docUri ?? undefined}
      defaultValue={value}
      onChange={(v) => onChange(v ?? "")}
      onMount={handleMount}
      options={{
        minimap: { enabled: false },
        fontSize: 13,
        tabSize: 4,
        scrollBeyondLastLine: false,
        automaticLayout: true,
        padding: { top: 8 },
        fixedOverflowWidgets: true,
        formatOnPaste: false,
        "semanticHighlighting.enabled": true,
      }}
    />
  );
}
