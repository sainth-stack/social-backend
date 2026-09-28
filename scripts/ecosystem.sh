#!/usr/bin/env bash
# Compatibility wrapper — prefer ./scripts/start.sh and ./scripts/stop.sh

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-}" in
  up|start) exec "$DIR/start.sh" ;;
  down|stop) exec "$DIR/stop.sh" ;;
  *)
    echo "Use:"
    echo "  ./scripts/start.sh      # local start"
    echo "  ./scripts/stop.sh       # local stop"
    echo "  ./scripts/pm2-start.sh  # EC2 / PM2 start"
    echo "  ./scripts/pm2-stop.sh   # EC2 / PM2 stop"
    exit 1
    ;;
esac
