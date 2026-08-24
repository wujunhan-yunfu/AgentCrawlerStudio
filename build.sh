#!/usr/bin/env bash
# 生成 static/ 静态产物: 由 frontend/ 源码经 Vite 构建 (vite.config.ts outDir: ../static)
# 可选附带重新生成 Monaco 代码补全索引 (frontend/src/libApi.json / playwrightApi.json)
set -euo pipefail
cd "$(dirname "$0")"

# 1. 重新生成爬虫/后处理库与 Playwright 的代码补全索引(需先 uv sync 装好依赖)
if [ -x .venv/bin/python ]; then
  .venv/bin/python frontend/scripts/generate_lib_api.py
  .venv/bin/python frontend/scripts/generate_playwright_api.py
fi

# 2. 安装前端依赖(如未安装)
if [ ! -d frontend/node_modules ]; then
  npm install --prefix frontend
fi

# 3. 构建前端 -> static/
npm run build --prefix frontend

echo "已生成 static/ 静态产物"
