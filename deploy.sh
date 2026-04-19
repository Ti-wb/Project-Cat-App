#!/usr/bin/env bash
# deploy.sh - 同步程式碼到本地 deploy 目錄並重啟服務
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR/deploy"
PUBLIC_PORT="${PUBLIC_PORT:-8900}"

echo "🚀 Deploying Cat App..."

mkdir -p "$DEPLOY_DIR"

# 同步程式碼（排除資料目錄與 git 相關）
rsync -av --delete \
  --exclude='backend/data/' \
  --exclude='data/' \
  --exclude='deploy/' \
  --exclude='.git/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='flutter_app/' \
  --exclude='*.md' \
  --exclude='.claude/' \
  --exclude='.gitignore' \
  "$SCRIPT_DIR/" "$DEPLOY_DIR/"

echo "📦 Building & restarting services..."
cd "$DEPLOY_DIR"
PUBLIC_PORT="$PUBLIC_PORT" docker compose build --no-cache api sync
PUBLIC_PORT="$PUBLIC_PORT" docker compose up -d api nginx

echo "✅ Deploy done!"
echo ""

HEALTH_URL="http://localhost:${PUBLIC_PORT}/health"
HEALTH_TIMEOUT_SECONDS=30
HEALTH_BACKOFF_SECONDS=1
HEALTH_MAX_BACKOFF_SECONDS=5
HEALTH_DEADLINE=$((SECONDS + HEALTH_TIMEOUT_SECONDS))

while true; do
  health_response="$(mktemp)"

  if curl --fail --silent --show-error "$HEALTH_URL" > "$health_response"; then
    if python3 -m json.tool < "$health_response"; then
      rm -f "$health_response"
      exit 0
    fi
    echo "Health probe returned invalid JSON, retrying..."
  else
    echo "Health probe failed, retrying..."
  fi

  rm -f "$health_response"

  if (( SECONDS >= HEALTH_DEADLINE )); then
    echo "ERROR: health probe did not succeed within ${HEALTH_TIMEOUT_SECONDS}s: $HEALTH_URL" >&2
    exit 1
  fi

  sleep "$HEALTH_BACKOFF_SECONDS"
  if (( HEALTH_BACKOFF_SECONDS < HEALTH_MAX_BACKOFF_SECONDS )); then
    HEALTH_BACKOFF_SECONDS=$((HEALTH_BACKOFF_SECONDS * 2))
    if (( HEALTH_BACKOFF_SECONDS > HEALTH_MAX_BACKOFF_SECONDS )); then
      HEALTH_BACKOFF_SECONDS=$HEALTH_MAX_BACKOFF_SECONDS
    fi
  fi
done
