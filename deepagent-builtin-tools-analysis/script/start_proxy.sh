#!/usr/bin/env bash
# 프록시를 백그라운드로 실행하고 PID를 저장한다.
# 사용법: bash script/start_proxy.sh
# 종료:   bash script/stop_proxy.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$SCRIPT_DIR/.proxy.pid"
LOG_FILE="$SCRIPT_DIR/dataset/proxy.log"

mkdir -p "$SCRIPT_DIR/dataset"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "[proxy] 이미 실행 중 (pid=$(cat "$PID_FILE"))"
  exit 0
fi

cd "$SCRIPT_DIR"
nohup uv run python module/proxy_logger.py > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
sleep 2

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "[proxy] 시작됨 (pid=$(cat "$PID_FILE"))"
  echo "[proxy] 로그 → $LOG_FILE"
  PORT_NUM=$(python3 -c "import os; print(os.getenv('PROXY_PORT','8082'))" 2>/dev/null || echo "8082")
echo "[proxy] Next → .env에 ANTHROPIC_BASE_URL=http://localhost:$PORT_NUM 추가 후 Claude Code 재시작"
else
  echo "[proxy] Failed to start. 로그 확인: $LOG_FILE"
  exit 1
fi
