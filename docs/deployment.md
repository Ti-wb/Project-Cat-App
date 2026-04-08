# Deployment Guide

本文件描述的是目前 repo 已落地的部署方式，而不是初版規劃稿。現行系統由三個 Docker Compose service 組成：

- `api`: FastAPI API
- `nginx`: 靜態圖片服務
- `sync`: 手動或排程執行的資料同步工作

## 部署架構

```text
repo/
  ├── backend/
  ├── nginx/
  ├── docker-compose.yml
  └── deploy.sh
         |
         | rsync
         v
deploy/
  ├── backend/data/cats.db
  ├── backend/data/images/{dataset_version}/{animal_id}.png
  ├── docker-compose.yml
  ├── backend/
  └── nginx/
```

### 重要行為

- `deploy.sh` 會把 repo 同步到 `./deploy/`
- `backend/data/` 不會被覆蓋，避免清掉既有資料庫與圖片
- API 對外只讀目前 published 的 `dataset_version`
- 圖片也採版本化目錄，不再是單層 `images/{id}.png`

## 前置需求

- Docker
- Docker Compose
- `rsync`
- 可寫入 `./deploy/` 目錄

## 部署指令

```bash
chmod +x deploy.sh
./deploy.sh
```

`deploy.sh` 目前實際會做以下事情：

1. 建立 `./deploy/`
2. 以 `rsync` 同步程式碼到 `./deploy/`
3. 排除以下內容，不覆蓋或不同步：
   - `backend/data/`
   - `.git/`
   - `.env`
   - `flutter_app/`
   - `*.md`
4. 進入 `./deploy/`
5. 重建 `api` image
6. 啟動或更新 `api` 與 `nginx`
7. 呼叫 `http://localhost:8900/health` 做基本驗證

### 部署後檢查

```bash
cd ./deploy
docker compose ps
curl http://localhost:8900/health
curl http://localhost:8901/health
```

## 手動操作

```bash
cd ./deploy

# 查看服務狀態
docker compose ps

# 查看 API logs
docker compose logs -f api

# 查看 nginx logs
docker compose logs -f nginx

# 手動執行資料同步
docker compose run --rm sync

# 重啟單一服務
docker compose restart api
docker compose restart nginx
```

## 每日資料同步

建議透過 cron 執行：

```bash
crontab -e
```

加入：

```cron
0 3 * * * cd ./deploy && docker compose run --rm sync >> /var/log/cat-sync.log 2>&1
```

### 同步流程重點

目前 `sync` 的實際行為不是直接覆蓋線上資料，而是：

1. 建立新的 `dataset_version`
2. 抓取政府 API 所有資料
3. 寫入新的 staged dataset
4. 下載或重用圖片到 `backend/data/images/{dataset_version}/`
5. 若同步成功，才切換 `current_published_version`
6. 清除舊版本資料

這代表同步失敗時，既有 API 仍會繼續提供舊的 published dataset。

## Ports

| Service | Port | 說明 |
| --- | --- | --- |
| API | `8900` | FastAPI，對應 `http://localhost:8900` |
| Images | `8901` | nginx，對應 `http://localhost:8901` |

### API / 圖片範例

```bash
curl http://localhost:8900/health
curl http://localhost:8900/api/cats
```

實際圖片路徑格式為：

```text
http://localhost:8901/images/{dataset_version}/{animal_id}.png
```

不是早期文件中的：

```text
/images/{id}.png
```

## Cloudflare 配置建議

### DNS

1. `cat-api.yourdomain.com` 指向 VM 或反向代理
2. `cat-images.yourdomain.com` 指向 VM 或反向代理

### Reverse Proxy

- `cat-api.yourdomain.com` -> `localhost:8900`
- `cat-images.yourdomain.com` -> `localhost:8901`

### Cache

- `cat-images.yourdomain.com/images/*`:
  `Cache Everything`
- Edge TTL:
  7 天
- API 路徑不建議快取

目前 nginx 也會對 `/images/` 加上：

- `Cache-Control: public, max-age=604800, immutable`
- `Access-Control-Allow-Origin: *`

## 環境變數

Docker Compose 目前使用：

```env
DATABASE_PATH=/app/data/cats.db
```

這會對應到 compose volume：

```text
./backend/data:/app/data
```

### 注意

- repo 根目錄也存在 `.env`
- `deploy.sh` 目前會排除 `.env`，所以若部署環境需要額外變數，需在 `./deploy/` 內自行建立或維護

## 資料路徑

部署完成後，關鍵資料會位於：

- `./deploy/backend/data/cats.db`
- `./deploy/backend/data/images/{dataset_version}/...`

這些資料不應進 git，也不應在每次 deploy 時被覆蓋。

## 已知文件差異

這份文件已反映目前實作，但仍有幾點值得注意：

- `deploy.sh` 目前不會同步任何 `*.md`，所以更新文件不會自動帶到 `./deploy/`
- `deploy.sh` 只會 `build --no-cache api`，`sync` 共用同一個 backend image，因此通常會跟著更新；若未來 compose 結構改變，這裡也要一起調整
- 現在沒有自動化 smoke test，health check 只驗證 API 是否存活，不保證當次同步成功
