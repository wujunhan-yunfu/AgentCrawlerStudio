import react from "@vitejs/plugin-react";
import { createRequire } from "node:module";
import { defineConfig, type Plugin } from "vite";

const require = createRequire(import.meta.url);

// @codingame/monaco-vscode-api 的 package.json exports 使用了通配符子路径,
// 部分 vite 版本解析失败, 这里用 Node 的 require.resolve 兜底。
function monacoVscodeAlias(): Plugin {
  return {
    name: "monaco-vscode-alias",
    resolveId(id) {
      if (/^@codingame\/monaco-vscode-api\/vscode\/(?!src\/)/.test(id)) {
        try {
          return require.resolve(id);
        } catch {
          return null;
        }
      }
      if (/^@codingame\/monaco-vscode-api\/vscode\/src\//.test(id)) {
        return require.resolve(id) ?? null;
      }
      return null;
    },
  };
}

export default defineConfig({
  plugins: [react(), monacoVscodeAlias()],
  base: "/",
  build: {
    outDir: "../static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 6000,
    target: "ES2022",
  },
  server: {
    port: 5173,
    proxy: {
      "/api/v1": {
        target: "http://127.0.0.1:8080",
        ws: true,
      },
    },
  },
});
