#!/usr/bin/env bash
# deploy.sh - 同步程式碼到本地 deploy 目錄並重啟服務
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR/deploy"

echo "🚀 Deploying Cat App..."

mkdir -p "$DEPLOY_DIR"

# 同步程式碼（排除資料目錄與 git 相關）
rsync -av --delete \
  --exclude='backend/data/' \
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
docker compose build --no-cache api
docker compose up -d api nginx

echo "✅ Deploy done!"
echo ""
curl -s http://localhost:8900/health | python3 -m json.tool
