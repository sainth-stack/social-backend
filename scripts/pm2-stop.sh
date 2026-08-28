#!/usr/bin/env bash
# Stop and remove all social-media PM2 processes.
#
# Usage (from backend/):
#   ./scripts/pm2-stop.sh
#
# Pair with:
#   pm2 start scripts/pm2.ecosystem.config.cjs

set -euo pipefail

pm2 delete \
  social-media-api \
  social-media-worker \
  social-media-beat \
  social-media-frontend \
  2>/dev/null || true

echo "Social media PM2 apps removed."
