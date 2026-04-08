# Deployment Guide

## 架構概覽

```
.                                   ← 開發 / Git
        │
        │  ./deploy.sh
        ▼
./deploy/                            ← 部署 (Docker Compose)
  ├── backend/data/cats.db            ← SQLite (不進 git)
  └── backend/data/images/            ← 貓咪照片 (不進 git)
```

## 部署指令

```bash
cd .
chmod +x deploy.sh   # 首次執行前
./deploy.sh
```

deploy.sh 會：
1. rsync 程式碼到 `./deploy/`（保留 `data/` 目錄不覆蓋）
2. rebuild `api` image
3. restart `api` + `nginx` 服務
4. 印出 health check 結果

## 手動操作

```bash
cd ./deploy

# 查看服務狀態
docker compose ps

# 查看 API logs
docker compose logs -f api

# 手動執行資料同步
docker compose run --rm sync

# 重啟單一服務
docker compose restart api
```

## 每日資料同步 (Cron)

```bash
# 編輯 crontab
crontab -e

# 加入以下排程（每天 03:00 同步）
0 3 * * * cd ./deploy && docker compose run --rm sync >> /var/log/cat-sync.log 2>&1
```

## 服務 Ports

| Service | Port | 說明 |
|---------|------|------|
| API | `8900` | FastAPI — `http://localhost:8900` |
| Images | `8901` | nginx 靜態圖片 — `http://localhost:8901/images/{id}.png` |

## Cloudflare 配置

1. DNS → CNAME `cat-api.yourdomain.com` → VM IP (proxied)
2. DNS → CNAME `cat-images.yourdomain.com` → VM IP (proxied)
3. Cache Rules → `/images/*` → Cache Everything, edge TTL 7d

反向代理 port mapping（在 VM 上用 nginx/caddy 轉發）：
- `cat-api.yourdomain.com` → `localhost:8900`
- `cat-images.yourdomain.com` → `localhost:8901`

## 環境變數

部署目錄的 `.env`（不進 git）：

```
DATABASE_PATH=/app/data/cats.db
```

如需新增環境變數，直接編輯 `./deploy/.env`。
