#!/usr/bin/env bash
# Copy OpsBrain social/AI/storage credentials into backend/.env
# Does NOT print secret values.
#
# Usage (from backend/):
#   ./scripts/copy-credentials.sh
#   OPSBRAIN_ENV=/path/to/.env ./scripts/copy-credentials.sh
set -euo pipefail

BACKEND="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$BACKEND/.." && pwd)"
OPSBRAIN_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
SRC="${OPSBRAIN_ENV:-$OPSBRAIN_ROOT/OpsBrain-Backend/.env}"
DEST="$BACKEND/.env"
EXAMPLE="$BACKEND/.env.example"

if [[ ! -f "$SRC" ]]; then
  echo "Source .env not found: $SRC"
  exit 1
fi

if [[ ! -f "$DEST" ]]; then
  cp "$EXAMPLE" "$DEST"
  echo "Created $DEST from .env.example"
fi

KEYS=(
  AZURE_OPENAI_API_KEY
  AZURE_OPENAI_ENDPOINT
  AZURE_OPENAI_API_VERSION
  AZURE_OPENAI_DEPLOYMENT
  AZURE_OPENAI_IMAGE_DEPLOYMENT
  AZURE_OPENAI_IMAGE_API_VERSION
  AZURE_OPENAI_VIDEO_DEPLOYMENT
  AZURE_OPENAI_VIDEO_API_VERSION
  AZURE_OPENAI_VIDEO_ENDPOINT
  AZURE_OPENAI_VIDEO_API_KEY
  VIDEO_GENERATION_ENABLED
  AZURE_STORAGE_CONNECTION_STRING
  AZURE_STORAGE_ACCOUNT_NAME
  AZURE_STORAGE_ACCOUNT_KEY
  AZURE_STORAGE_CONTAINER_NAME
  META_APP_ID
  META_APP_SECRET
  META_API_VERSION
  META_INSTAGRAM_APP_ID
  META_INSTAGRAM_APP_SECRET
  LINKEDIN_CLIENT_ID
  LINKEDIN_CLIENT_SECRET
  LINKEDIN_ORGANIZATION_SCOPES
  X_CLIENT_ID
  X_CLIENT_SECRET
  CREDENTIAL_ENCRYPTION_KEY
)

copied=0
for key in "${KEYS[@]}"; do
  val=$(grep -E "^${key}=" "$SRC" | tail -n1 | cut -d= -f2- || true)
  if [[ -z "${val}" ]]; then
    echo "skip (missing in source): $key"
    continue
  fi
  if grep -qE "^${key}=" "$DEST"; then
    sed -i.bak "s|^${key}=.*|${key}=${val}|" "$DEST"
  else
    echo "${key}=${val}" >> "$DEST"
  fi
  copied=$((copied + 1))
  echo "copied: $key"
done

rm -f "$DEST.bak"
echo "Done. Copied $copied keys into $DEST"
echo "Still set manually: DATABASE_URL, REDIS_URL, JWT_SECRET_KEY, ADMIN_*, FRONTEND_URL, OAuth redirect URIs"
