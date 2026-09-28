#!/usr/bin/env bash
# EC2: start API + worker + beat + frontend with PM2
# Usage (from backend/):  ./scripts/pm2-start.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing .env — on EC2 copy: cp .env.production .env" >&2
  exit 1
fi

mkdir -p "$ROOT/.run"

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.venv/bin/activate"
fi

echo "Ensuring MongoDB indexes..."
"$ROOT/scripts/migrate.sh"

pm2 start "$ROOT/scripts/pm2.ecosystem.config.cjs"
pm2 save
pm2 status

echo ""
echo "Production running (PM2):"
echo "  API       http://127.0.0.1:5000"
echo "  Frontend  http://127.0.0.1:5001"
echo "  Stop      ./scripts/pm2-stop.sh"
