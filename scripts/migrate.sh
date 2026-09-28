#!/usr/bin/env bash
# Ensure MongoDB is reachable and create indexes.
#
# Usage:
#   ./scripts/migrate.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.venv/bin/activate"
elif [[ -f "$ROOT/venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/venv/bin/activate"
fi

if [[ -z "${MONGODB_URL:-}" ]]; then
  echo "ERROR: MONGODB_URL is not set in .env" >&2
  exit 1
fi

echo "[migrate] connecting to MongoDB and ensuring indexes"
export PYTHONPATH="${PYTHONPATH:-}:$ROOT"
python -c "from app.core.database import init_db; init_db()"
echo "[migrate] done"
