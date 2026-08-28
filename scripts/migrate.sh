#!/usr/bin/env bash
# Run Alembic migrations.
#
# Usage:
#   ./scripts/migrate.sh
#   ./scripts/migrate.sh current
#   ./scripts/migrate.sh upgrade head

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

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL is not set in .env" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  set -- upgrade head
fi

echo "[migrate] running alembic $*"
export PYTHONPATH="${PYTHONPATH:-}:$ROOT"
exec alembic "$@"
