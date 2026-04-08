# Project Cat App — 專案結構與技術規劃

## Context

建立一個跨平台 (iOS/Android) Flutter App，方便瀏覽台灣公立收容所的貓咪資訊。由於政府 Open Data API 回應較慢（尤其圖片），需要每日爬取資料並自行 Host，搭配 Cloudflare CDN 加速。

---

## Tech Stack

| Layer | 選擇 | 理由 |
|-------|------|------|
| App | **Flutter** | 跨平台需求 |
| 狀態管理 | **Riverpod** | 輕量、type-safe、適合篩選場景 |
| Backend | **Python + FastAPI** | VM 已有 Python，單檔即可完成 API server |
| 資料庫 | **SQLite** | 資料量小（~百筆），零維運成本 |
| 圖片服務 | **nginx** (靜態檔) | 直接 serve 下載好的圖片，搭配 Cloudflare CDN |
| 排程 | **cron** (系統) | 每日一行指令，無需額外 daemon |
| 部署 | **Docker Compose** | 三個輕量 service，預估 ~60MB RAM |

---

## 資料來源

- **API**: `https://data.moa.gov.tw/Service/OpenData/TransService.aspx?UnitId=QcbUEzN6E6DL&IsTransData=1&animal_kind=貓`
- 無需認證，JSON 格式，每日更新
- 篩選參數: `$top`, `$skip`, `animal_kind=貓`
- 圖片來源: `pet.gov.tw`（PNG）
- 共 27 個欄位，含 animal_id, shelter info, 照片 URL 等

---

## 資料流

```
政府 API (data.moa.gov.tw)          deploy/ directory                    Flutter App
         |                                    |                              |
         |  每日 03:00 cron                    |                              |
         |  ──────────────>  sync.py          |                              |
         |                   ├ fetch JSON     |                              |
         |                   ├ upsert SQLite  |                              |
pet.gov.tw ────(下載圖片)──> ├ save images/   |                              |
                             └ 清除已領養資料  |                              |
                                    |                                        |
                              FastAPI ◄──── GET /api/cats ──────────────────  |
                              (讀 SQLite)  ── JSON response ──────────────>  |
                                    |                                        |
                              nginx ◄───── GET /images/{id}.png ───────────  |
                              (靜態圖片)     (經 Cloudflare CDN)              |
```

---

## 目錄結構

```
.
├── docker-compose.yml
├── .env
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt        # fastapi, uvicorn, httpx
│   ├── main.py                 # FastAPI app (~150 行)
│   ├── sync.py                 # 爬蟲 + 圖片下載
│   ├── database.py             # SQLite helper
│   ├── models.py               # Pydantic models
│   └── data/
│       ├── cats.db             # SQLite (volume mount)
│       └── images/             # 貓咪照片 (volume mount)
│
├── nginx/
│   └── nginx.conf              # 靜態圖片 + cache headers
│
└── flutter_app/                # Flutter 專案（Phase 2）
```

---

## Backend API 端點

| Method | Path | 說明 |
|--------|------|------|
| GET | `/api/cats` | 貓咪列表，支援 `shelter`, `area_pkid`, `colour`, `age`, `sex`, `bodytype`, `sterilization`, `q` 篩選 |
| GET | `/api/cats/{id}` | 單隻貓咪詳情 |
| GET | `/api/shelters` | 收容所列表（供篩選 dropdown） |
| GET | `/api/filters` | 可用篩選值（顏色、地區等） |
| GET | `/health` | 健康檢查 |

---

## SQLite Schema

```sql
CREATE TABLE cats (
  animal_id         INTEGER PRIMARY KEY,
  animal_subid      TEXT,
  animal_place      TEXT,
  animal_variety    TEXT,
  animal_sex        TEXT,       -- M / F
  animal_bodytype   TEXT,       -- SMALL / MEDIUM / LARGE
  animal_colour     TEXT,
  animal_age        TEXT,       -- ADULT / CHILD
  animal_sterilization TEXT,    -- T / F
  animal_bacterin   TEXT,       -- T / F
  animal_foundplace TEXT,
  animal_status     TEXT,
  animal_remark     TEXT,
  animal_opendate   TEXT,
  animal_update     TEXT,
  animal_createtime TEXT,
  shelter_name      TEXT,
  shelter_address   TEXT,
  shelter_tel       TEXT,
  album_file        TEXT,       -- 原始 pet.gov.tw URL
  local_image       TEXT,       -- 本地檔名 (e.g. "439514.png")
  area_pkid         INTEGER,
  shelter_pkid      INTEGER,
  synced_at         TEXT
);
```

---

## Docker Compose 服務

| Service | Port | 說明 |
|---------|------|------|
| `api` | 8900 → 8000 | FastAPI (uvicorn) |
| `nginx` | 8901 → 80 | 靜態圖片服務 |
| `sync` | — | 按需執行的爬蟲 (profiles: sync) |

預估資源: ~60MB RAM，對 2CPU/8GB VM 無壓力。

---

## Cloudflare 配置

- `cat-api.yourdomain.com` → VM:8900 (proxied)
- `cat-images.yourdomain.com` → VM:8901 (proxied)
- 圖片路徑 `/images/*` 設定 Cache Everything，edge TTL 7 天
- API 路徑不快取

---

## sync.py 邏輯

1. GET 政府 API，取得所有貓咪 JSON
2. INSERT OR REPLACE 到 SQLite
3. 下載有 `album_file` 但尚未快取的圖片到 `data/images/{animal_id}.png`（限速 1 req/sec）
4. 刪除 API 中已不存在的紀錄（已領養/移除）
5. 清除孤立的圖片檔案
6. Cron: `0 3 * * * cd ./deploy && docker compose run --rm sync`

---

## 實作範圍與順序

### Phase 1 — Backend（已完成）

1. 建立目錄結構 + docker-compose.yml
2. 實作 `sync.py` — 爬蟲 + SQLite + 圖片下載
3. 實作 `main.py` — FastAPI 端點
4. 設定 nginx.conf
5. 測試 backend（跑 sync → 驗證 API → 檢查圖片）
6. 設定每日 cron job

### Phase 2 — Flutter App（待定，使用者設計中）

> **暫緩實作**：使用者正在規劃整體 UI/UX 與 User Story。
> App 部分可能包含額外帳號系統等需求，待設計完成後再進行。
> Backend API 設計需預留彈性以支援未來帳號相關功能（如收藏、追蹤等）。

Flutter 核心套件: `flutter_riverpod`, `dio`, `go_router`, `cached_network_image`, `freezed` + `json_serializable`

---

## 驗證方式

- **Backend**: 執行 `sync.py` 後，確認 `cats.db` 有資料、`images/` 有圖片、`curl /api/cats` 回傳正確 JSON
- **圖片**: 透過 nginx URL 可存取圖片
- **Cron**: 確認每日 03:00 自動執行 sync
