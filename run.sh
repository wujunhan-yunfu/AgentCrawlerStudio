#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -f static/index.html ]; then
  echo "static/ 产物缺失, 自动构建前端..."
  bash build.sh
fi
if [ -f .app.pid ] && kill -0 "$(cat .app.pid)" 2>/dev/null; then
  echo "已在运行 (pid $(cat .app.pid))"
  exit 0
fi
setsid uv run python -m backend.main "$@" > .app.log 2>&1 < /dev/null &
echo $! > .app.pid
echo "已启动 (pid $(cat .app.pid)), 日志: .app.log"
