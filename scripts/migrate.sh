#!/usr/bin/env bash
# Ensure MongoDB is reachable and create indexes.
# Loads .env via Python (safe for & in MONGODB_URL — do not "source .env" in bash).
#
# Usage:
#   ./scripts/migrate.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "ERROR: Missing $ROOT/.env" >&2
  exit 1
fi

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.venv/bin/activate"
elif [[ -f "$ROOT/venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/venv/bin/activate"
fi

echo "[migrate] connecting to MongoDB and ensuring indexes"
export PYTHONPATH="${PYTHONPATH:-}:$ROOT"
python - <<'PY'
from app.core.config import settings
from app.core.database import init_db

url = (settings.mongodb_url or "").strip()
if not url:
    raise SystemExit("ERROR: MONGODB_URL is not set in .env")

init_db()
PY
echo "[migrate] done"
