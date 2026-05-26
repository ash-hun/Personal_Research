#!/usr/bin/env bash
# 프록시 프로세스를 종료한다.
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$SCRIPT_DIR/.proxy.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "[proxy] Not running (PID file not found)"
  exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  rm "$PID_FILE"
  echo "[proxy] Stopped (pid=$PID)"
else
  rm "$PID_FILE"
  echo "[proxy] Was not running"
fi
