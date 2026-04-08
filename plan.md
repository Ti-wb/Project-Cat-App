# Project Cat App — 規劃與實作現況

## 專案背景

Project Cat App 的目標，是建立一個跨平台 Flutter App，讓使用者可以更順暢地瀏覽台灣公立收容所的貓咪資訊。

由於政府 Open Data API 與圖片來源常有以下問題：

- 回應速度不穩
- 圖片載入慢
- 不適合直接作為行動 App 的上游

因此本專案採用「先同步、再對外服務」的做法：

- 每日從政府 API 抓取最新資料
- 將資料整理後寫入本地 SQLite
- 將圖片快取到本地靜態目錄
- 由 FastAPI 與 nginx 對 App 提供穩定 API 與圖片 URL

## 專案範圍

### 已實作範圍

- 後端 API
- 資料同步腳本
- SQLite schema 與初始化
- nginx 圖片靜態服務
- Docker Compose
- 本地部署腳本

### 尚未實作範圍

- Flutter App
- UI / UX
- 帳號系統
- 收藏、追蹤等使用者功能
- 自動化測試與 CI/CD

## 系統設計

```text
政府 Open Data API
        |
        | sync.py
        v
staged dataset_version
        |
        | publish
        v
current_published_version
        |
   +----+----+
   |         |
   v         v
 FastAPI    nginx
   |         |
   +----+----+
        |
        v
   Flutter App（未開始）
```

## 技術選型

| Layer | 技術 | 理由 |
| --- | --- | --- |
| App | Flutter | 跨平台 iOS / Android |
| State | Riverpod | 原始規劃保留，待 App 階段確認 |
| Backend | Python + FastAPI | 實作簡潔，適合輕量 API |
| Database | SQLite | 資料量小，維運成本低 |
| Image Serving | nginx | 適合快取靜態圖片 |
| Scheduling | cron | 每日同步即可 |
| Deployment | Docker Compose | 組成單純，部署成本低 |

## 資料來源

- API:
  `https://data.moa.gov.tw/Service/OpenData/TransService.aspx?UnitId=QcbUEzN6E6DL&IsTransData=1&animal_kind=貓`
- 上游資料格式：
  JSON
- 圖片來源：
  `pet.gov.tw` 等上游網域

## 目前實作與原始規劃的差異

這份文件原本是早期規劃稿，但目前程式已進化成更完整的版本。最大的差異如下：

### 1. 不再是單表直接覆蓋

早期規劃假設同步時直接更新單一 `cats` 表。

目前實作改為：

- `dataset_cats` 儲存版本化資料
- `app_state.current_published_version` 指向目前對外版本
- 新版本同步完成後才切換 publish

這樣可以避免同步中途資料不完整時影響 API。

### 2. 圖片已改成版本化路徑

早期規劃中的圖片 URL 為：

```text
/images/{id}.png
```

目前實作實際為：

```text
/images/{dataset_version}/{animal_id}.png
```

### 3. 已加入同步執行紀錄與移除記錄

目前資料庫除了主資料外，還有：

- `sync_runs`
- `dataset_image_fetch_state`
- `cat_removals`

這些都不是初版規劃中的內容，但已是目前系統的重要部分。

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
│       └── images/
├── nginx/
│   └── nginx.conf
└── data/
    └── cats.db
```

### 目錄角色

- `backend/main.py`: API
- `backend/sync.py`: 同步與圖片處理
- `backend/database.py`: schema 與 DB helper
- `backend/models.py`: Pydantic model
- `backend/data/`: Docker volume 掛載點
- `data/`: 非 Docker 本機執行時可能使用的 SQLite 路徑

## API 規劃與現況

目前實作中的端點如下：

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/health` | 健康檢查、資料筆數、published version |
| GET | `/api/cats` | 貓咪列表與篩選 |
| GET | `/api/cats/{animal_id}` | 單隻貓咪詳情 |
| GET | `/api/shelters` | 收容所列表 |
| GET | `/api/filters` | 篩選值清單 |

### `GET /api/cats` 支援參數

- `shelter`
- `area_pkid`
- `colour`
- `age`
- `sex`
- `bodytype`
- `sterilization`
- `status`
- `q`
- `limit`
- `offset`

## SQLite 設計

### 目前關鍵資料表

| Table | 用途 |
| --- | --- |
| `cats` | 舊版相容資料表，供 bootstrap 使用 |
| `dataset_cats` | 版本化主資料表 |
| `dataset_image_fetch_state` | 圖片抓取狀態 |
| `sync_runs` | 同步工作執行紀錄 |
| `cat_removals` | 上游已移除資料的歷史紀錄 |
| `app_state` | 目前 published version 等狀態 |

### 設計重點

- API 只讀 `dataset_cats` 中目前 published 的版本
- 每次同步會產生新的 `dataset_version`
- 舊資料會在 publish 後清理
- 若舊的單表 `cats` 仍存在資料，可 bootstrap 成 `bootstrap` 版本

## sync.py 邏輯

目前同步流程的真實邏輯如下：

1. 檢查是否有卡太久的 `running` sync，必要時標記失敗
2. 產生新的 `dataset_version`
3. 分頁抓取政府 API
4. 將資料寫入 `dataset_cats`
5. 若圖片來源未變且舊圖仍存在，優先複用舊圖
6. 其餘圖片加入下載隊列
7. 下載圖片，失敗時盡量 fallback 到舊圖
8. 記錄此次同步的統計資訊到 `sync_runs`
9. 成功後切換 `current_published_version`
10. 記錄已不在上游中的貓咪到 `cat_removals`
11. 清除舊版本 dataset 與其圖片目錄

## 安全與穩定性設計

目前同步程式已加入以下機制：

- metadata 與 image 使用不同限速
- 429 / 5xx retry 與 backoff
- 圖片 redirect 次數限制
- 圖片 host allowlist
- DNS 解析後拒絕 private / loopback / reserved IP
- stale running sync 自動標記失敗

## Docker Compose 服務

| Service | Port | 說明 |
| --- | --- | --- |
| `api` | `8900 -> 8000` | FastAPI |
| `nginx` | `8901 -> 80` | 靜態圖片服務 |
| `sync` | 無對外 port | 手動或排程觸發 |

## 部署與排程規劃

### 現況

- 已有 `deploy.sh`
- 已有 `docs/deployment.md`
- 已有可用的 cron 指令範例

### 建議排程

```cron
0 3 * * * cd ./deploy && docker compose run --rm sync >> /var/log/cat-sync.log 2>&1
```

## Phase 規劃

### Phase 1 — Backend

狀態：已基本完成

已完成內容：

- API
- 同步流程
- 版本化 dataset
- 圖片快取
- 部署腳本

仍可補強：

- 測試
- 監控
- 文件一致性

### Phase 2 — Flutter App

狀態：尚未開始

預計會包含：

- 貓咪列表頁
- 篩選與搜尋
- 詳情頁
- 圖片快取
- 可能的收藏 / 帳號功能

原始規劃中的 Flutter 套件候選仍可參考：

- `flutter_riverpod`
- `dio`
- `go_router`
- `cached_network_image`
- `freezed`
- `json_serializable`

## 驗證方式

### 已做過的最低限度驗證

- 後端 Python 檔案可通過 `py_compile`

### 建議手動驗證

1. 執行 `docker compose run --rm sync`
2. 確認 `backend/data/cats.db` 產生資料
3. 呼叫 `curl http://localhost:8900/health`
4. 呼叫 `curl http://localhost:8900/api/cats`
5. 驗證回傳的 `image_url` 含 `dataset_version`
6. 用 nginx URL 開啟圖片

## 後續優先事項

1. 為同步流程與 API 建立基本測試
2. 將部署驗證從單純 health check 升級為 smoke test
3. 決定 Flutter App 的資料模型與 user flow
4. 再開始 App 端實作
