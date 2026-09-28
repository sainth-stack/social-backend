#!/usr/bin/env bash
# EC2: stop PM2 processes
# Usage (from backend/):  ./scripts/pm2-stop.sh

set -euo pipefail

pm2 delete \
  social-media-api \
  social-media-worker \
  social-media-beat \
  social-media-frontend \
  2>/dev/null || true

echo "PM2 apps stopped."
