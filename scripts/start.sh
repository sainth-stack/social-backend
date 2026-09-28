#!/usr/bin/env bash
# Local: start API + Celery worker + beat
# Usage (from backend/):  ./scripts/start.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8000}"
RUN_DIR="$ROOT/.run"
mkdir -p "$RUN_DIR"

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.venv/bin/activate"
elif [[ -f "$ROOT/venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/venv/bin/activate"
else
  echo "Missing venv. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing .env — copy from .env.example" >&2
  exit 1
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
  CELERY_POOL=solo
else
  CELERY_POOL=prefork
fi

start_one() {
  local name=$1
  shift
  local pidfile="$RUN_DIR/$name.pid"
  local logfile="$RUN_DIR/$name.log"

  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pidfile"))"
    return 0
  fi

  if [[ "$name" == "api" ]]; then
    local port_pid
    port_pid=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [[ -n "$port_pid" ]]; then
      kill "$port_pid" 2>/dev/null || true
      sleep 1
    fi
  fi

  : >"$logfile"
  nohup "$@" >>"$logfile" 2>&1 &
  echo $! >"$pidfile"
  sleep 1
  if kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "started $name  (log: $logfile)"
  else
    echo "failed to start $name — tail $logfile" >&2
    tail -20 "$logfile" >&2 || true
    exit 1
  fi
}

start_one api uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload
start_one worker celery -A workers.celery_app:celery_app worker -l info \
  -Q social_publish,social_analytics,social_maintenance \
  -P "$CELERY_POOL" -n "worker@%h"
start_one beat celery -A workers.celery_app:celery_app beat -l info

echo ""
echo "Local running:"
echo "  API      http://localhost:$PORT"
echo "  Health   http://localhost:$PORT/health"
echo "  Docs     http://localhost:$PORT/docs"
echo "  Frontend cd ../frontend && npm run dev"
echo "  Stop     ./scripts/stop.sh"
