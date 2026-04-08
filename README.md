# Project Cat App

台灣公立收容所貓咪資料彙整服務。這個 repo 目前實作的是後端資料同步、API 與圖片靜態服務，目標是支援後續的 Flutter App，以更快、更穩定的方式瀏覽政府開放資料中的貓咪資訊。

專案的核心想法是：

- 上游政府 Open Data API 可以提供貓咪資料，但回應速度與圖片存取體驗不穩定。
- 因此本專案每日同步資料到自管 SQLite，並將圖片快取到本地，由 nginx 提供靜態檔，再搭配 Cloudflare CDN。
- App 端只讀取本專案提供的 API 與圖片，不直接打政府 API。

## 專案目標

- 提供適合行動 App 使用的貓咪查詢 API
- 降低上游 API 與圖片來源的延遲
- 讓資料同步與對外提供服務解耦
- 為後續 Flutter App、收藏功能、帳號功能保留擴充空間

## 目前開發狀態

目前倉庫的真實狀態如下：

- 已完成後端第一階段基礎建設：FastAPI、SQLite、資料同步腳本、nginx 圖片服務、Docker Compose、部署腳本
- 已實作版本化資料集同步流程，不是單純覆蓋資料表
- 已實作圖片下載、重試、來源驗證、舊版本圖片重用、已移除貓咪記錄追蹤
- Flutter App 尚未開始，repo 內也沒有 `flutter_app/`
- 自動化測試目前尚未建立

如果用一句話總結現況：這是一個「後端資料服務已可運作、前端 App 尚未開工」的專案。

## 系統架構

```text
政府 Open Data API
  data.moa.gov.tw
        |
        |  每日同步
        v
  backend/sync.py
        |
        |  staged dataset_version
        v
  SQLite (dataset_cats / sync_runs / cat_removals ...)
        |
        |  publish current_published_version
        +----------------------+
        |                      |
        v                      v
  FastAPI                  nginx
  /api/*                   /images/*
        |                      |
        +----------+-----------+
                   |
                   v
           Flutter App（規劃中）
```

### 為什麼現在的實作比初版規劃更完整

最初的 `plan.md` 描述的是單次同步後直接覆蓋資料的版本；目前程式已經進一步實作為「版本化發布」：

- 每次同步會先建立新的 `dataset_version`
- 新資料先寫進 staged dataset
- 圖片補齊後才切換 `current_published_version`
- API 永遠只讀目前已發布的版本
- 同步失敗時不會污染目前對外服務的資料

這個設計比初版規劃更安全，也更適合每天定時同步的場景。

## 技術選型

| Layer | 技術 | 說明 |
| --- | --- | --- |
| Backend API | FastAPI | 提供查詢 API |
| 同步程式 | Python + httpx | 抓政府資料、下載圖片、寫入 SQLite |
| Database | SQLite | 輕量、零維運，適合目前資料量 |
| Image Server | nginx | 提供靜態圖片與快取標頭 |
| Deployment | Docker Compose | 啟動 `api`、`nginx`、`sync` |
| Scheduling | cron | 每日執行同步 |

## 資料來源

- 政府 Open Data API:
  `https://data.moa.gov.tw/Service/OpenData/TransService.aspx?UnitId=QcbUEzN6E6DL&IsTransData=1&animal_kind=貓`
- 圖片來源主要來自 `pet.gov.tw` 等上游網址
- 上游資料不需認證，JSON 格式

## Repository 結構

```text
.
├── README.md
├── plan.md
├── docker-compose.yml
├── deploy.sh
├── docs/
│   └── deployment.md
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py
│   ├── sync.py
│   ├── database.py
│   ├── models.py
│   └── data/
│       ├── .gitkeep
│       └── images/
├── nginx/
│   └── nginx.conf
└── data/
    └── cats.db
```

### 目錄說明

- `backend/main.py`: FastAPI API 入口
- `backend/sync.py`: 同步政府資料、下載圖片、發布新 dataset
- `backend/database.py`: SQLite schema 與 helper
- `backend/models.py`: Pydantic response models
- `backend/data/`: Docker Compose 預設 volume 掛載位置
- `data/`: 本機非 Docker 執行時的預設 SQLite 路徑
- `docs/deployment.md`: 部署流程與手動操作紀錄

## API 概覽

### `GET /health`

回傳服務健康狀態、目前對外發布的資料版本，以及該版本貓咪數量。

### `GET /api/cats`

貓咪列表查詢，支援：

- `shelter`
- `area_pkid`
- `colour`
- `age`
- `sex`
- `bodytype`
- `sterilization`
- `status`，預設為 `OPEN`
- `q`
- `limit`
- `offset`

### `GET /api/cats/{animal_id}`

取得單隻貓咪完整資訊。

### `GET /api/shelters`

取得收容所列表與每個收容所的貓咪數量。

### `GET /api/filters`

回傳目前 published dataset 中可用的篩選值：

- `colours`
- `ages`
- `sexes`
- `bodytypes`
- `sterilizations`

## 圖片 URL 設計

目前圖片不是平面的 `/images/{id}.png`，而是帶版本號：

```text
/images/{dataset_version}/{animal_id}.png
```

這對版本化同步很重要，因為：

- 新版資料同步時可以先建立新圖片目錄
- 發布前後不會互相覆蓋
- Cloudflare 與瀏覽器快取更容易控制

## 資料庫設計重點

目前資料庫已超過初版單表設計，實際上包含多個用途不同的表：

- `cats`: 舊版相容用資料表，啟動時可 bootstrap 到新版資料集
- `dataset_cats`: 真正提供 API 查詢的版本化資料表
- `dataset_image_fetch_state`: 每張圖片的抓取狀態
- `sync_runs`: 每次同步執行紀錄
- `cat_removals`: 已從上游資料消失的貓咪歷史紀錄
- `app_state`: 目前 published dataset version 等狀態

### 現行同步流程

1. 建立新的 `dataset_version`
2. 從政府 API 分頁抓取所有貓咪資料
3. 將資料寫入 `dataset_cats`
4. 若圖片未變更，優先重用目前 published dataset 的圖片
5. 其餘圖片進入下載佇列
6. 下載失敗時，若舊圖存在則退回重用舊圖
7. 全部完成後才切換 `current_published_version`
8. 記錄已被上游移除的貓咪
9. 清除舊版本資料與圖片

## 安全與穩定性設計

`sync.py` 目前已包含一些實際可用的防護：

- API 與圖片請求分開限速
- 針對 429 / 5xx 進行 retry 與 backoff
- 圖片下載限制 redirect 次數
- 圖片 URL 僅允許白名單網域
- 解析 DNS 後拒絕 private、loopback、reserved 等 IP，避免 SSRF 類風險
- 若發現卡住超過 6 小時的同步工作，會標記為失敗

## 本機啟動

### 方式一：使用 Docker Compose

啟動 API 與圖片服務：

```bash
docker compose up --build -d api nginx
```

手動執行一次同步：

```bash
docker compose run --rm sync
```

檢查服務：

```bash
curl http://localhost:8900/health
curl http://localhost:8900/api/cats
curl http://localhost:8900/nginx-health
```

### 方式二：直接本機執行 Python

安裝依賴：

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

執行同步：

```bash
python sync.py
```

啟動 API：

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

注意：

- 非 Docker 模式下，`DATABASE_PATH` 預設是 `./data/cats.db`，相對於 `backend/`
- Docker 模式下，`DATABASE_PATH=/app/data/cats.db`，對應到 repo 中的 `backend/data/`

## Docker Compose 服務

| Service | Port | 說明 |
| --- | --- | --- |
| `nginx` | `8900 -> 80` | 對外唯一入口，轉發 `/api` 並提供 `/images` |
| `api` | 無對外 port | FastAPI，僅供 nginx 內部反代 |
| `sync` | 無對外 port | 手動或排程執行同步 |

## 部署

部署腳本：

```bash
chmod +x deploy.sh
./deploy.sh
```

`deploy.sh` 會：

1. 將程式同步到 `./deploy/`
2. 保留 `backend/data/` 不被覆蓋
3. 重建 `api` 與 `sync` 使用的 backend image
4. 啟動或更新 `api` 與 `nginx`
5. 呼叫 `/health` 驗證服務

更完整的部署操作可參考 `docs/deployment.md`。

## 排程同步

建議用 cron 每天同步一次：

```cron
0 3 * * * cd ./deploy && docker compose run --rm sync >> /var/log/cat-sync.log 2>&1
```

## Cloudflare / CDN 建議

- API 與圖片走同一個對外 origin
- 圖片路徑可設定 `Cache Everything`
- API 路徑不建議快取
- 目前 nginx 已為 `/images/` 設定 7 天快取標頭

## 已完成與待完成項目

### 已完成

- 後端 API
- SQLite schema 與初始化
- 資料同步腳本
- 圖片下載與快取
- 版本化發布流程
- Docker Compose
- nginx 靜態圖片服務
- 基本部署腳本與部署文件

### 尚未完成

- Flutter App
- 帳號系統
- 收藏、追蹤等使用者功能
- 自動化測試
- CI/CD
- 監控與告警

## 開發風險與注意事項

- `plan.md` 仍保留初版規劃，部分內容已與目前實作不同，應以程式碼為準
- `plan.md` 與部分舊文件仍保留早期規劃，若與目前單一 origin 拓撲不同，應以程式碼為準
- repo 目前同時存在 `backend/data/` 與 `data/`，前者是 Docker volume 目標，後者偏向本機執行時使用，開發時要避免混淆
- 目前沒有測試覆蓋，修改同步與 schema 時需要手動驗證

## 建議的下一步

如果要繼續推進這個專案，優先順序建議如下：

1. 補上 README 中提到的文件落差，讓 `plan.md` / `docs` 與實作一致
2. 為同步流程與 API 加入基本自動化測試
3. 決定 Flutter App 的資訊架構與 user flow
4. 再開始 App 端串接 API

## 驗證現況

我已檢查目前 repo 的後端 Python 檔案可通過語法編譯：

```bash
python3 -m py_compile backend/main.py backend/sync.py backend/database.py backend/models.py
```

這代表目前至少在語法層面是可載入的；但是否能完整啟動，仍需在有 Docker 與網路可用的環境中實際跑一次同步與 API。
