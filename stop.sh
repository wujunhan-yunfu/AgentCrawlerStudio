#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ ! -f .app.pid ]; then echo "没有运行中的进程"; exit 0; fi
pid=$(cat .app.pid)
kill -TERM -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
for _ in $(seq 1 30); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.2
done
kill -0 "$pid" 2>/dev/null && kill -KILL -- -"$pid" 2>/dev/null || true
rm -f .app.pid
echo "已停止"
