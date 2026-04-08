"""
sync.py - Sync cat data from Taiwan government API to local SQLite database.
Usage: python sync.py
"""
import os
import time
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ── Configuration ──────────────────────────────────────────────────────────────
DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/cats.db")
IMAGE_DIR = Path(os.path.dirname(DATABASE_PATH)) / "images"
API_BASE = (
    "https://data.moa.gov.tw/Service/OpenData/TransService.aspx"
    "?UnitId=QcbUEzN6E6DL&IsTransData=1&animal_kind=貓"
)
PAGE_SIZE = 1000
DOWNLOAD_DELAY = 0.5
USER_AGENT = "CatAdoptionApp/1.0"
REQUEST_TIMEOUT = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ── DB helpers ─────────────────────────────────────────────────────────────────
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cats (
            animal_id         INTEGER PRIMARY KEY,
            animal_subid      TEXT,
            animal_place      TEXT,
            animal_variety    TEXT,
            animal_sex        TEXT,
            animal_bodytype   TEXT,
            animal_colour     TEXT,
            animal_age        TEXT,
            animal_sterilization TEXT,
            animal_bacterin   TEXT,
            animal_foundplace TEXT,
            animal_status     TEXT,
            animal_remark     TEXT,
            animal_opendate   TEXT,
            animal_closeddate TEXT,
            animal_update     TEXT,
            animal_createtime TEXT,
            shelter_name      TEXT,
            shelter_address   TEXT,
            shelter_tel       TEXT,
            album_file        TEXT,
            local_image       TEXT,
            area_pkid         INTEGER,
            shelter_pkid      INTEGER,
            synced_at         TEXT
        )
    """)
    conn.commit()


# ── API fetching ───────────────────────────────────────────────────────────────
def fetch_all_cats(client: httpx.Client) -> list[dict]:
    records: list[dict] = []
    skip = 0
    while True:
        url = f"{API_BASE}&$top={PAGE_SIZE}&$skip={skip}"
        log.info("Fetching page skip=%d ...", skip)
        resp = client.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        records.extend(page)
        log.info("  Got %d records (total so far: %d)", len(page), len(records))
        if len(page) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return records


# ── Image downloading ──────────────────────────────────────────────────────────
def download_image(client: httpx.Client, animal_id: int, url: str) -> bool:
    dest = IMAGE_DIR / f"{animal_id}.png"
    if dest.exists():
        return False  # already downloaded
    try:
        resp = client.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            log.warning("Skip image animal_id=%d: HTTP %d", animal_id, resp.status_code)
            return False
        dest.write_bytes(resp.content)
        return True
    except Exception as exc:
        log.warning("Skip image animal_id=%d: %s", animal_id, exc)
        return False


# ── Main sync logic ────────────────────────────────────────────────────────────
def sync():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

    conn = get_connection()
    init_db(conn)

    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        log.info("Fetching data from government API ...")
        records = fetch_all_cats(client)
        log.info("Total records from API: %d", len(records))

        now = datetime.now(timezone.utc).isoformat()
        api_ids: set[int] = set()

        added = updated = 0

        for r in records:
            try:
                animal_id = int(r.get("animal_id", 0))
            except (ValueError, TypeError):
                continue
            if animal_id == 0:
                continue
            api_ids.add(animal_id)

            row = {
                "animal_id": animal_id,
                "animal_subid": r.get("animal_subid"),
                "animal_place": r.get("animal_place"),
                "animal_variety": r.get("animal_Variety") or r.get("animal_variety"),
                "animal_sex": r.get("animal_sex"),
                "animal_bodytype": r.get("animal_bodytype"),
                "animal_colour": r.get("animal_colour"),
                "animal_age": r.get("animal_age"),
                "animal_sterilization": r.get("animal_sterilization"),
                "animal_bacterin": r.get("animal_bacterin"),
                "animal_foundplace": r.get("animal_foundplace"),
                "animal_status": r.get("animal_status"),
                "animal_remark": r.get("animal_remark"),
                "animal_opendate": r.get("animal_opendate"),
                "animal_closeddate": r.get("animal_closeddate"),
                "animal_update": r.get("animal_update"),
                "animal_createtime": r.get("animal_createtime"),
                "shelter_name": r.get("shelter_name"),
                "shelter_address": r.get("shelter_address"),
                "shelter_tel": r.get("shelter_tel"),
                "album_file": r.get("album_file"),
                "area_pkid": _int_or_none(r.get("animal_area_pkid")),
                "shelter_pkid": _int_or_none(r.get("animal_shelter_pkid")),
                "synced_at": now,
            }

            existing = conn.execute(
                "SELECT animal_id, local_image FROM cats WHERE animal_id = ?", (animal_id,)
            ).fetchone()

            if existing:
                # preserve local_image if already set
                row["local_image"] = existing["local_image"]
                conn.execute(
                    """UPDATE cats SET
                        animal_subid=:animal_subid, animal_place=:animal_place,
                        animal_variety=:animal_variety, animal_sex=:animal_sex,
                        animal_bodytype=:animal_bodytype, animal_colour=:animal_colour,
                        animal_age=:animal_age, animal_sterilization=:animal_sterilization,
                        animal_bacterin=:animal_bacterin, animal_foundplace=:animal_foundplace,
                        animal_status=:animal_status, animal_remark=:animal_remark,
                        animal_opendate=:animal_opendate, animal_closeddate=:animal_closeddate,
                        animal_update=:animal_update, animal_createtime=:animal_createtime,
                        shelter_name=:shelter_name, shelter_address=:shelter_address,
                        shelter_tel=:shelter_tel, album_file=:album_file,
                        local_image=:local_image,
                        area_pkid=:area_pkid, shelter_pkid=:shelter_pkid, synced_at=:synced_at
                    WHERE animal_id=:animal_id""",
                    row,
                )
                updated += 1
            else:
                row["local_image"] = None
                conn.execute(
                    """INSERT INTO cats (
                        animal_id, animal_subid, animal_place, animal_variety,
                        animal_sex, animal_bodytype, animal_colour, animal_age,
                        animal_sterilization, animal_bacterin, animal_foundplace,
                        animal_status, animal_remark, animal_opendate, animal_closeddate,
                        animal_update, animal_createtime, shelter_name, shelter_address,
                        shelter_tel, album_file, local_image, area_pkid, shelter_pkid, synced_at
                    ) VALUES (
                        :animal_id, :animal_subid, :animal_place, :animal_variety,
                        :animal_sex, :animal_bodytype, :animal_colour, :animal_age,
                        :animal_sterilization, :animal_bacterin, :animal_foundplace,
                        :animal_status, :animal_remark, :animal_opendate, :animal_closeddate,
                        :animal_update, :animal_createtime, :shelter_name, :shelter_address,
                        :shelter_tel, :album_file, :local_image, :area_pkid, :shelter_pkid, :synced_at
                    )""",
                    row,
                )
                added += 1

        conn.commit()

        # ── Delete removed animals ───────────────────────────────────────────
        existing_ids = {
            row[0] for row in conn.execute("SELECT animal_id FROM cats").fetchall()
        }
        removed_ids = existing_ids - api_ids
        deleted = 0
        for rid in removed_ids:
            img_path = IMAGE_DIR / f"{rid}.png"
            if img_path.exists():
                img_path.unlink()
            conn.execute("DELETE FROM cats WHERE animal_id = ?", (rid,))
            deleted += 1
        if deleted:
            conn.commit()
            log.info("Deleted %d removed animals from DB", deleted)

        # ── Download images ──────────────────────────────────────────────────
        to_download = conn.execute(
            "SELECT animal_id, album_file FROM cats WHERE album_file IS NOT NULL AND album_file != '' AND local_image IS NULL"
        ).fetchall()

        log.info("Images to download: %d", len(to_download))
        downloaded = 0
        for row in to_download:
            aid = row["animal_id"]
            img_url = row["album_file"]
            success = download_image(client, aid, img_url)
            if success:
                conn.execute(
                    "UPDATE cats SET local_image = ? WHERE animal_id = ?",
                    (f"{aid}.png", aid),
                )
                downloaded += 1
                if downloaded % 10 == 0:
                    conn.commit()
                    log.info("  Downloaded %d images so far ...", downloaded)
                time.sleep(DOWNLOAD_DELAY)

        conn.commit()

    conn.close()

    log.info(
        "Sync complete — added: %d, updated: %d, deleted: %d, images downloaded: %d",
        added,
        updated,
        deleted,
        downloaded,
    )


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    sync()
