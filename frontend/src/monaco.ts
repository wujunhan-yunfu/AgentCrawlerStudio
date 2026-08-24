import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import { initialize } from "@codingame/monaco-vscode-api";
import getConfigurationServiceOverride from "@codingame/monaco-vscode-configuration-service-override";
import getLanguagesServiceOverride from "@codingame/monaco-vscode-languages-service-override";
import getLogServiceOverride from "@codingame/monaco-vscode-log-service-override";
import getModelServiceOverride from "@codingame/monaco-vscode-model-service-override";
import { registerPythonIntelliSense } from "./pythonIntelliSense";
import { registerPythonTheme, PYTHON_THEME } from "./pythonTheme";
import { registerPythonCodeActions, registerPythonFormatting } from "./pythonActions";

export { PYTHON_THEME };

(globalThis as unknown as {
  MonacoEnvironment: {
    getWorker(_moduleId: string, _label: string): Worker;
  };
}).MonacoEnvironment = {
  getWorker(_moduleId: string, _label: string): Worker {
    return new editorWorker();
  },
};

loader.config({ monaco });

// 初始化 monaco-vscode-api 服务层, 必须在任何编辑器/语言服务访问之前调用,
// 否则 vscode-languageclient 无法感知 Monaco 的文档模型。
const servicesReady = initialize({
  ...getModelServiceOverride(),
  ...getLanguagesServiceOverride(),
  ...getConfigurationServiceOverride(),
  ...getLogServiceOverride(),
});

export function ensureMonacoServices(): Promise<void> {
  return servicesReady;
}

registerPythonIntelliSense();
registerPythonTheme();
registerPythonCodeActions();
registerPythonFormatting();
