import { MonacoLanguageClient, type MonacoLanguageClientOptions } from "monaco-languageclient";
import { CloseAction, ErrorAction } from "vscode-languageclient";
import { toSocket, WebSocketMessageReader, WebSocketMessageWriter } from "vscode-ws-jsonrpc";
import { api } from "./utils/api";

let client: MonacoLanguageClient | null = null;
let docUri: string | null = null;

export function getDocUri(): string | null {
  return docUri;
}

function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${api("/ws/lsp")}`;
}

/** 从后端获取 LSP 工作区信息, 确保编辑器模型 URI 与后端一致。 */
export async function ensureLspInfo(): Promise<string> {
  if (docUri) return docUri;
  const r = await fetch(api("/lsp/info"));
  if (!r.ok) throw new Error(`/lsp/info ${r.status}`);
  const info = (await r.json()) as { doc_uri: string };
  docUri = info.doc_uri;
  return docUri;
}

function createTransports(): Promise<MonacoLanguageClientOptions["messageTransports"]> {
  const webSocket = new WebSocket(wsUrl());
  return new Promise((resolve, reject) => {
    webSocket.onerror = (e) => {
      console.error("LSP WebSocket error", e);
      reject(new Error("LSP WebSocket 连接失败"));
    };
    webSocket.onopen = () => {
      const socket = toSocket(webSocket);
      resolve({
        reader: new WebSocketMessageReader(socket),
        writer: new WebSocketMessageWriter(socket),
      });
    };
  });
}

function startClient(): void {
  if (client) return;
  void createTransports().then(
    (messageTransports) => {
      client = new MonacoLanguageClient({
        name: "Python Language Client (pyright)",
        clientOptions: {
          documentSelector: ["python"],
          diagnosticCollectionName: "pyright",
          errorHandler: {
            error: () => ({ action: ErrorAction.Continue }),
            closed: () => ({ action: CloseAction.DoNotRestart }),
          },
        },
        messageTransports,
      });
      client.start().catch((e) => console.error("LSP client start failed", e));
    },
    (e) => {
      console.error("LSP connection failed, retrying in 3s", e);
      setTimeout(startClient, 3000);
    },
  );
}

/** 启动 pyright 语言客户端(幂等)。 */
export function startPythonLsp(): void {
  void ensureLspInfo().then(startClient);
}

export function disposePythonLsp(): Promise<void> | undefined {
  if (!client) return undefined;
  const c = client;
  client = null;
  return c.dispose();
}
