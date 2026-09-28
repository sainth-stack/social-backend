#!/usr/bin/env bash
# Local: stop API + Celery worker + beat
# Usage (from backend/):  ./scripts/stop.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/.run"
PORT="${PORT:-8000}"

stop_one() {
  local name=$1
  local pidfile="$RUN_DIR/$name.pid"
  [[ -f "$pidfile" ]] || return 0
  local pid
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
  echo "stopped $name"
}

stop_one beat
stop_one worker
stop_one api

port_pid=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
if [[ -n "$port_pid" ]]; then
  kill "$port_pid" 2>/dev/null || true
fi
pkill -f "celery -A workers.celery_app" 2>/dev/null || true

echo "Local services stopped."
