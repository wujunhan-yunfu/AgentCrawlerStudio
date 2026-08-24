import { useEffect, useState } from "react";
import * as monaco from "monaco-editor";

export interface Problem {
  severity: monaco.MarkerSeverity;
  message: string;
  source?: string;
  startLineNumber: number;
  startColumn: number;
}

/** 订阅 monaco markers(由 LSP publishDiagnostics 写入), 得到问题列表。 */
export function useProblems(model: monaco.editor.ITextModel | null): Problem[] {
  const [problems, setProblems] = useState<Problem[]>([]);

  useEffect(() => {
    if (!model) {
      setProblems([]);
      return;
    }
    const update = () => {
      const markers = monaco.editor
        .getModelMarkers({ resource: model.uri })
        .filter(
          (m) =>
            m.severity === monaco.MarkerSeverity.Error ||
            m.severity === monaco.MarkerSeverity.Warning ||
            m.severity === monaco.MarkerSeverity.Info,
        )
        .map((m) => ({
          severity: m.severity,
          message: m.message,
          source: m.source,
          startLineNumber: m.startLineNumber,
          startColumn: m.startColumn,
        }));
      setProblems(markers);
    };
    const markerSub = monaco.editor.onDidChangeMarkers(update);
    const contentSub = model.onDidChangeContent(() => update());
    update();
    return () => {
      markerSub.dispose();
      contentSub.dispose();
    };
  }, [model]);

  return problems;
}
