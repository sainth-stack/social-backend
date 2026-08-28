#!/usr/bin/env bash
# OpsBrain Social Media — local dev ecosystem (same pattern as OpsBrain-Backend/scripts/ecosystem.sh)
#
# Usage (from backend/):
#   ./scripts/ecosystem.sh up       # api + celery worker + beat
#   ./scripts/ecosystem.sh down
#   ./scripts/ecosystem.sh status
#   ./scripts/ecosystem.sh logs api
#   ./scripts/ecosystem.sh api      # foreground uvicorn only
#
# Single API with hot reload:
#   RELOAD=1 ./scripts/ecosystem.sh api
#
# Environment:
#   PORT=8000 RELOAD=1 START_FRONTEND=1 ./scripts/ecosystem.sh up

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUN_DIR="$ROOT/.run"
mkdir -p "$RUN_DIR"

cmd="${1:-status}"
PORT="${PORT:-8000}"
RELOAD="${RELOAD:-0}"
FRONTEND_PORT="${FRONTEND_PORT:-3001}"
START_FRONTEND="${START_FRONTEND:-0}"

REPO_ROOT="$(cd "$ROOT/.." && pwd)"
FRONTEND="$REPO_ROOT/frontend"
CELERY_QUEUES="${CELERY_QUEUES:-social_publish,social_analytics,social_maintenance}"

if [[ "$(uname -s)" == "Darwin" ]]; then
  export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
  CELERY_POOL="${CELERY_POOL:-solo}"
else
  CELERY_POOL="${CELERY_POOL:-prefork}"
fi

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.venv/bin/activate"
elif [[ -f "$ROOT/venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/venv/bin/activate"
else
  echo "venv not found — run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing $ROOT/.env — copy from .env.example" >&2
  exit 1
fi

check_redis() {
  if command -v redis-cli >/dev/null 2>&1 && redis-cli ping 2>/dev/null | grep -q PONG; then
    echo "Redis: ok (local)"
    return 0
  fi
  if python - <<'PY' 2>/dev/null
from workers.redis.client import ping_redis
raise SystemExit(0 if ping_redis() else 1)
PY
  then
    echo "Redis: ok (REDIS_URL)"
    return 0
  fi
  echo "WARNING: Redis not reachable — Celery, auth rate limits, and content planner need Redis." >&2
  echo "         macOS: brew services start redis" >&2
}

start_bg() {
  local name=$1
  shift
  local pidfile="$RUN_DIR/$name.pid"
  local logfile="$RUN_DIR/$name.log"

  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pidfile"))"
    return 0
  fi
  rm -f "$pidfile"
  : >"$logfile"

  if [[ "$name" == "api" ]]; then
    local port_pid
    port_pid=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [[ -n "$port_pid" ]]; then
      echo "Port $PORT in use by pid $port_pid — stopping stale process..."
      kill "$port_pid" 2>/dev/null || true
      sleep 1
      kill -9 "$port_pid" 2>/dev/null || true
    fi
  fi

  if [[ "$name" == "frontend" ]]; then
    local port_pid
    port_pid=$(lsof -ti :"$FRONTEND_PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [[ -n "$port_pid" ]]; then
      echo "Port $FRONTEND_PORT in use by pid $port_pid — stopping stale process..."
      kill "$port_pid" 2>/dev/null || true
      sleep 1
      kill -9 "$port_pid" 2>/dev/null || true
    fi
  fi

  nohup "$@" >>"$logfile" 2>&1 &
  echo $! >"$pidfile"
  sleep 1
  if kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "Started $name (pid $(cat "$pidfile"), log: $logfile)"
  else
    echo "Failed to start $name — check $logfile" >&2
    tail -20 "$logfile" >&2 || true
    rm -f "$pidfile"
    return 1
  fi
}

stop_one() {
  local name=$1
  local pidfile="$RUN_DIR/$name.pid"
  if [[ -f "$pidfile" ]]; then
    local pid
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      local i=0
      while kill -0 "$pid" 2>/dev/null && [[ $i -lt 8 ]]; do
        sleep 1
        ((i++))
      done
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$pidfile"
    echo "Stopped $name"
  fi
}

kill_stale_processes() {
  local port_pid
  port_pid=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
  if [[ -n "$port_pid" ]]; then
    echo "Clearing stale listener on port $PORT (pid $port_pid)..."
    kill "$port_pid" 2>/dev/null || true
    sleep 1
    kill -9 "$port_pid" 2>/dev/null || true
  fi

  if pgrep -f "celery -A workers.celery_app" >/dev/null 2>&1; then
    echo "Stopping orphaned Celery processes..."
    pkill -f "celery -A workers.celery_app" 2>/dev/null || true
    sleep 1
    pkill -9 -f "celery -A workers.celery_app" 2>/dev/null || true
  fi
}

UVICORN_CMD=(uvicorn app.main:app --host 0.0.0.0 --port "$PORT")
if [[ "$RELOAD" == "1" ]]; then
  UVICORN_CMD+=(--reload)
fi

run_api_foreground() {
  kill_stale_processes
  echo "Starting API on http://localhost:$PORT (Ctrl+C to stop)"
  exec "${UVICORN_CMD[@]}"
}

case "$cmd" in
  up)
    kill_stale_processes
    check_redis

    start_bg api "${UVICORN_CMD[@]}"
    start_bg worker celery -A workers.celery_app:celery_app worker -l info \
      -Q "$CELERY_QUEUES" \
      -P "$CELERY_POOL" -n "worker@%h" \
      --without-heartbeat --without-gossip --without-mingle
    start_bg beat celery -A workers.celery_app:celery_app beat -l info

    if [[ "$START_FRONTEND" == "1" && -d "$FRONTEND/node_modules" ]]; then
      (cd "$FRONTEND" && start_bg frontend npm run dev -- -p "$FRONTEND_PORT")
    fi

    echo ""
    echo "OpsBrain Social Media running:"
    echo "  API:        http://localhost:$PORT"
    echo "  Health:     http://localhost:$PORT/health"
    echo "  API prefix: http://localhost:$PORT/api/v1"
    if [[ "$START_FRONTEND" == "1" ]]; then
      echo "  Frontend:   http://localhost:$FRONTEND_PORT"
    else
      echo "  Frontend:   cd ../frontend && npm run dev"
      echo "              (or START_FRONTEND=1 ./scripts/ecosystem.sh up)"
    fi
    echo "  Processes:  api + worker + beat"
    if [[ "$(uname -s)" == "Darwin" ]]; then
      echo "  Celery pool: solo (macOS)"
    fi
    echo "  Logs:       tail -f $RUN_DIR/*.log"
    echo "  Stop:       ./scripts/ecosystem.sh down"
    ;;

  down)
    stop_one beat
    stop_one worker
    stop_one frontend
    stop_one api
    kill_stale_processes
    ;;

  status)
    for name in api worker beat frontend; do
      pidfile="$RUN_DIR/$name.pid"
      if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "$name: running (pid $(cat "$pidfile"))"
      else
        echo "$name: stopped"
      fi
    done
    if curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then
      echo "health: ok"
    else
      echo "health: api not reachable on :$PORT"
    fi
    ;;

  logs)
    tail -f "$RUN_DIR/${2:-api}.log"
    ;;

  api)
    run_api_foreground
    ;;

  *)
    echo "Usage: $0 {up|down|status|logs [api|worker|beat|frontend]|api}"
    echo ""
    echo "  up              Start api + celery worker + beat in background"
    echo "  api             Start api only in foreground"
    echo "  down            Stop all background processes"
    echo "  status          Show process + health status"
    echo "  logs [name]     Tail logs (default: api)"
    echo ""
    echo "Examples:"
    echo "  RELOAD=1 $0 api"
    echo "  RELOAD=1 $0 up"
    echo "  START_FRONTEND=1 $0 up"
    exit 1
    ;;
esac
