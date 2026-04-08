"""
sync.py - Sync cat data from Taiwan government API to local SQLite database.
Usage: python sync.py
"""
import hashlib
import json
import logging
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from database import DATABASE_PATH, get_connection, init_db

# ── Configuration ──────────────────────────────────────────────────────────────
IMAGE_DIR = Path(os.path.dirname(DATABASE_PATH)) / "images"
API_BASE = (
    "https://data.moa.gov.tw/Service/OpenData/TransService.aspx"
    "?UnitId=QcbUEzN6E6DL&IsTransData=1&animal_kind=貓"
)
PAGE_SIZE = 1000
USER_AGENT = "CatAdoptionApp/1.0"
REQUEST_TIMEOUT = 30
METADATA_INTERVAL = 2.0
IMAGE_INTERVAL = 1.0
METADATA_MAX_ATTEMPTS = 4
IMAGE_MAX_ATTEMPTS = 3
METADATA_BACKOFFS = (2.0, 4.0, 8.0)
IMAGE_BACKOFFS = (2.0, 5.0)
IMAGE_FAILURE_COOLDOWN = 6 * 60 * 60
MAX_IMAGES_PER_RUN = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncConfig:
    page_size: int = PAGE_SIZE
    metadata_interval: float = METADATA_INTERVAL
    image_interval: float = IMAGE_INTERVAL
    metadata_max_attempts: int = METADATA_MAX_ATTEMPTS
    image_max_attempts: int = IMAGE_MAX_ATTEMPTS
    request_timeout: int = REQUEST_TIMEOUT
    image_failure_cooldown: int = IMAGE_FAILURE_COOLDOWN
    max_images_per_run: int = MAX_IMAGES_PER_RUN


@dataclass(frozen=True)
class DownloadResult:
    outcome: str
    error_code: str | None = None
    content: bytes | None = None


class RateLimiter:
    def __init__(self, intervals: dict[str, float]):
        self.intervals = intervals
        self.last_request_at: dict[str, float] = {}

    def wait(self, lane: str):
        interval = self.intervals[lane]
        now = time.monotonic()
        last_at = self.last_request_at.get(lane)
        if last_at is not None:
            sleep_for = interval - (now - last_at)
            if sleep_for > 0:
                time.sleep(sleep_for)
        self.last_request_at[lane] = time.monotonic()


class UpstreamClient:
    def __init__(self, client: httpx.Client, limiter: RateLimiter, config: SyncConfig):
        self.client = client
        self.limiter = limiter
        self.config = config

    def fetch_metadata_page(self, skip: int) -> list[dict]:
        url = f"{API_BASE}&$top={self.config.page_size}&$skip={skip}"
        response = self._request_with_retry(
            lane="metadata",
            url=url,
            max_attempts=self.config.metadata_max_attempts,
            backoffs=METADATA_BACKOFFS,
        )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to decode metadata page skip={skip}") from exc
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected payload type for metadata page skip={skip}")
        return payload

    def download_image(self, url: str) -> DownloadResult:
        for attempt in range(1, self.config.image_max_attempts + 1):
            self.limiter.wait("image")
            try:
                response = self.client.get(url, timeout=self.config.request_timeout)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                error_code = exc.__class__.__name__
                if attempt >= self.config.image_max_attempts:
                    return DownloadResult(outcome="retryable_failure", error_code=error_code)
                self._sleep_before_retry(
                    lane="image",
                    attempt=attempt,
                    backoffs=IMAGE_BACKOFFS,
                    retry_after=None,
                    reason=error_code,
                )
                continue

            status = response.status_code
            if status == 200:
                return DownloadResult(outcome="success", content=response.content)
            if status in (404, 410):
                return DownloadResult(outcome="terminal_failure", error_code=f"HTTP_{status}")
            if self._is_retryable_status(status):
                if attempt >= self.config.image_max_attempts:
                    return DownloadResult(
                        outcome="retryable_failure",
                        error_code=f"HTTP_{status}",
                    )
                self._sleep_before_retry(
                    lane="image",
                    attempt=attempt,
                    backoffs=IMAGE_BACKOFFS,
                    retry_after=self._retry_after_seconds(response),
                    reason=f"HTTP {status}",
                )
                continue
            return DownloadResult(outcome="terminal_failure", error_code=f"HTTP_{status}")

        return DownloadResult(outcome="retryable_failure", error_code="UNKNOWN")

    def _request_with_retry(
        self,
        lane: str,
        url: str,
        max_attempts: int,
        backoffs: tuple[float, ...],
    ) -> httpx.Response:
        for attempt in range(1, max_attempts + 1):
            self.limiter.wait(lane)
            try:
                response = self.client.get(url, timeout=self.config.request_timeout)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(f"Request failed after retries: {url}") from exc
                self._sleep_before_retry(
                    lane=lane,
                    attempt=attempt,
                    backoffs=backoffs,
                    retry_after=None,
                    reason=exc.__class__.__name__,
                )
                continue

            if response.status_code < 400:
                return response
            if not self._is_retryable_status(response.status_code):
                response.raise_for_status()
            if attempt >= max_attempts:
                response.raise_for_status()
            self._sleep_before_retry(
                lane=lane,
                attempt=attempt,
                backoffs=backoffs,
                retry_after=self._retry_after_seconds(response),
                reason=f"HTTP {response.status_code}",
            )
        raise RuntimeError(f"Request exhausted retries: {url}")

    def _sleep_before_retry(
        self,
        lane: str,
        attempt: int,
        backoffs: tuple[float, ...],
        retry_after: float | None,
        reason: str,
    ):
        delay = retry_after
        if delay is None:
            backoff = backoffs[min(attempt - 1, len(backoffs) - 1)]
            delay = backoff + random.uniform(0, 1)
        log.warning("Retrying %s request after %.2fs due to %s", lane, delay, reason)
        time.sleep(delay)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return None
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            return None

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or status_code in (500, 502, 503, 504)


def fetch_all_cats(upstream: UpstreamClient, run_id: int) -> list[dict]:
    records: list[dict] = []
    skip = 0
    pages_fetched = 0
    while True:
        log.info("Fetching page skip=%d ...", skip)
        page = upstream.fetch_metadata_page(skip)
        pages_fetched += 1
        update_sync_run(
            run_id,
            pages_fetched=pages_fetched,
            records_seen=len(records) + len(page),
        )
        if not page:
            break
        records.extend(page)
        log.info("  Got %d records (total so far: %d)", len(page), len(records))
        if len(page) < upstream.config.page_size:
            break
        skip += upstream.config.page_size
    return records


def sync():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    init_db()

    conn = get_connection()
    headers = {"User-Agent": USER_AGENT}
    limiter = RateLimiter({"metadata": METADATA_INTERVAL, "image": IMAGE_INTERVAL})
    config = SyncConfig()
    run_id = create_sync_run(conn)

    try:
        with httpx.Client(headers=headers, follow_redirects=True) as client:
            upstream = UpstreamClient(client=client, limiter=limiter, config=config)
            log.info("Fetching data from government API ...")
            records = fetch_all_cats(upstream, run_id)
            log.info("Total records from API: %d", len(records))

            counts = upsert_cats(conn, records, run_id)
            snapshot_complete = counts["invalid_records"] == 0
            removed_count = 0
            run_status = "success"
            error_summary = None
            if snapshot_complete:
                removed_count = record_and_delete_removed_cats(conn, counts["api_ids"], run_id)
            else:
                run_status = "partial"
                error_summary = (
                    f"Skipped removals because {counts['invalid_records']} upstream records "
                    "had missing or invalid animal_id values"
                )
                log.warning(error_summary)
            counts["deleted"] = removed_count
            update_sync_run(
                run_id,
                records_upserted=counts["added"] + counts["updated"],
                records_removed=removed_count,
            )

            image_stats = sync_images(conn, upstream, config, run_id)
            finalize_sync_run(
                conn,
                run_id,
                status=run_status,
                error_summary=error_summary,
                images_attempted=image_stats["attempted"],
                images_succeeded=image_stats["succeeded"],
                images_failed=image_stats["failed"],
            )

            log.info(
                "Sync complete — added: %d, updated: %d, deleted: %d, images attempted: %d, images downloaded: %d, image failures: %d",
                counts["added"],
                counts["updated"],
                counts["deleted"],
                image_stats["attempted"],
                image_stats["succeeded"],
                image_stats["failed"],
            )
    except Exception as exc:
        finalize_sync_run(conn, run_id, status="failed", error_summary=str(exc))
        raise
    finally:
        conn.close()


def upsert_cats(conn: sqlite3.Connection, records: list[dict], run_id: int) -> dict[str, int | set[int]]:
    now = utc_now()
    api_ids: set[int] = set()
    added = updated = invalid_records = 0

    for record in records:
        animal_id = _safe_int(record.get("animal_id"))
        if not animal_id:
            invalid_records += 1
            continue
        api_ids.add(animal_id)

        row = {
            "animal_id": animal_id,
            "animal_subid": record.get("animal_subid"),
            "animal_place": record.get("animal_place"),
            "animal_variety": record.get("animal_Variety") or record.get("animal_variety"),
            "animal_sex": record.get("animal_sex"),
            "animal_bodytype": record.get("animal_bodytype"),
            "animal_colour": record.get("animal_colour"),
            "animal_age": record.get("animal_age"),
            "animal_sterilization": record.get("animal_sterilization"),
            "animal_bacterin": record.get("animal_bacterin"),
            "animal_foundplace": record.get("animal_foundplace"),
            "animal_status": record.get("animal_status"),
            "animal_remark": record.get("animal_remark"),
            "animal_opendate": record.get("animal_opendate"),
            "animal_closeddate": record.get("animal_closeddate"),
            "animal_update": record.get("animal_update"),
            "animal_createtime": record.get("animal_createtime"),
            "shelter_name": record.get("shelter_name"),
            "shelter_address": record.get("shelter_address"),
            "shelter_tel": record.get("shelter_tel"),
            "album_file": record.get("album_file"),
            "source_album_url": record.get("album_file"),
            "source_animal_update": record.get("animal_update"),
            "source_album_update": record.get("album_update"),
            "area_pkid": _safe_int(record.get("animal_area_pkid")),
            "shelter_pkid": _safe_int(record.get("animal_shelter_pkid")),
            "synced_at": now,
            "last_seen_at": now,
        }

        existing = conn.execute(
            """
            SELECT animal_id, local_image, first_seen_at, album_file, source_album_update
            FROM cats WHERE animal_id = ?
            """,
            (animal_id,),
        ).fetchone()

        if existing:
            row["local_image"] = existing["local_image"]
            row["first_seen_at"] = existing["first_seen_at"] or now
            conn.execute(
                """
                UPDATE cats SET
                    animal_subid=:animal_subid,
                    animal_place=:animal_place,
                    animal_variety=:animal_variety,
                    animal_sex=:animal_sex,
                    animal_bodytype=:animal_bodytype,
                    animal_colour=:animal_colour,
                    animal_age=:animal_age,
                    animal_sterilization=:animal_sterilization,
                    animal_bacterin=:animal_bacterin,
                    animal_foundplace=:animal_foundplace,
                    animal_status=:animal_status,
                    animal_remark=:animal_remark,
                    animal_opendate=:animal_opendate,
                    animal_closeddate=:animal_closeddate,
                    animal_update=:animal_update,
                    animal_createtime=:animal_createtime,
                    shelter_name=:shelter_name,
                    shelter_address=:shelter_address,
                    shelter_tel=:shelter_tel,
                    album_file=:album_file,
                    local_image=:local_image,
                    source_album_url=:source_album_url,
                    source_animal_update=:source_animal_update,
                    source_album_update=:source_album_update,
                    area_pkid=:area_pkid,
                    shelter_pkid=:shelter_pkid,
                    synced_at=:synced_at,
                    first_seen_at=:first_seen_at,
                    last_seen_at=:last_seen_at
                WHERE animal_id=:animal_id
                """,
                row,
            )
            updated += 1
        else:
            row["local_image"] = None
            row["first_seen_at"] = now
            conn.execute(
                """
                INSERT INTO cats (
                    animal_id, animal_subid, animal_place, animal_variety,
                    animal_sex, animal_bodytype, animal_colour, animal_age,
                    animal_sterilization, animal_bacterin, animal_foundplace,
                    animal_status, animal_remark, animal_opendate, animal_closeddate,
                    animal_update, animal_createtime, shelter_name, shelter_address,
                    shelter_tel, album_file, local_image, source_album_url,
                    source_animal_update, source_album_update, area_pkid, shelter_pkid,
                    synced_at, first_seen_at, last_seen_at
                ) VALUES (
                    :animal_id, :animal_subid, :animal_place, :animal_variety,
                    :animal_sex, :animal_bodytype, :animal_colour, :animal_age,
                    :animal_sterilization, :animal_bacterin, :animal_foundplace,
                    :animal_status, :animal_remark, :animal_opendate, :animal_closeddate,
                    :animal_update, :animal_createtime, :shelter_name, :shelter_address,
                    :shelter_tel, :album_file, :local_image, :source_album_url,
                    :source_animal_update, :source_album_update, :area_pkid, :shelter_pkid,
                    :synced_at, :first_seen_at, :last_seen_at
                )
                """,
                row,
            )
            added += 1

        if has_removed_image_source(existing, row):
            conn.execute(
                "UPDATE cats SET local_image = NULL WHERE animal_id = ?",
                (animal_id,),
            )
            conn.execute("DELETE FROM image_fetch_state WHERE animal_id = ?", (animal_id,))
        elif should_queue_image_refresh(existing, row):
            force_reset = should_force_image_refresh(existing, row)
            if should_clear_local_image(existing, row):
                conn.execute(
                    "UPDATE cats SET local_image = NULL WHERE animal_id = ?",
                    (animal_id,),
                )
            queue_image_refresh(
                conn,
                animal_id,
                row["source_album_url"],
                force_reset=force_reset,
            )

    conn.commit()
    update_sync_run(
        run_id,
        records_seen=len(api_ids),
        records_upserted=added + updated,
    )
    return {
        "api_ids": api_ids,
        "added": added,
        "updated": updated,
        "invalid_records": invalid_records,
    }


def should_queue_image_refresh(existing: sqlite3.Row | None, row: dict) -> bool:
    source_url = row["source_album_url"]
    if not source_url:
        return False
    local_image = row["local_image"]
    if not local_image:
        return True
    if not (IMAGE_DIR / local_image).exists():
        return True
    if existing is None:
        return True
    if existing["album_file"] != row["album_file"]:
        return True
    if existing["source_album_update"] != row["source_album_update"]:
        return True
    return False


def queue_image_refresh(
    conn: sqlite3.Connection,
    animal_id: int,
    source_url: str,
    force_reset: bool = False,
):
    source_url_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO image_fetch_state (
            animal_id, source_url_hash, last_attempt_at, last_success_at,
            failure_count, last_error_code, next_eligible_at, status
        ) VALUES (?, ?, NULL, NULL, 0, NULL, NULL, 'pending')
        ON CONFLICT(animal_id) DO UPDATE SET
            source_url_hash=excluded.source_url_hash,
            last_success_at=CASE
                WHEN image_fetch_state.source_url_hash != excluded.source_url_hash OR ? THEN NULL
                ELSE image_fetch_state.last_success_at
            END,
            failure_count=CASE
                WHEN image_fetch_state.source_url_hash != excluded.source_url_hash OR ? THEN 0
                ELSE image_fetch_state.failure_count
            END,
            last_error_code=CASE
                WHEN image_fetch_state.source_url_hash != excluded.source_url_hash OR ? THEN NULL
                ELSE image_fetch_state.last_error_code
            END,
            next_eligible_at=CASE
                WHEN image_fetch_state.source_url_hash != excluded.source_url_hash OR ? THEN NULL
                ELSE image_fetch_state.next_eligible_at
            END,
            status=CASE
                WHEN image_fetch_state.source_url_hash != excluded.source_url_hash OR ? THEN 'pending'
                ELSE image_fetch_state.status
            END
        """,
        (
            animal_id,
            source_url_hash,
            int(force_reset),
            int(force_reset),
            int(force_reset),
            int(force_reset),
            int(force_reset),
        ),
    )


def should_clear_local_image(existing: sqlite3.Row | None, row: dict) -> bool:
    if existing is None:
        return True
    local_image = row["local_image"]
    if not local_image:
        return False
    if not (IMAGE_DIR / local_image).exists():
        return True
    if existing["album_file"] != row["album_file"]:
        return True
    if existing["source_album_update"] != row["source_album_update"]:
        return True
    return False


def should_force_image_refresh(existing: sqlite3.Row | None, row: dict) -> bool:
    if existing is None:
        return False
    local_image = row["local_image"]
    if local_image and not (IMAGE_DIR / local_image).exists():
        return True
    if existing["album_file"] != row["album_file"]:
        return True
    if existing["source_album_update"] != row["source_album_update"]:
        return True
    return False


def has_removed_image_source(existing: sqlite3.Row | None, row: dict) -> bool:
    if existing is None:
        return False
    return bool(existing["album_file"]) and not row["album_file"]


def record_and_delete_removed_cats(conn: sqlite3.Connection, api_ids: set[int], run_id: int) -> int:
    existing_rows = conn.execute(
        "SELECT * FROM cats"
    ).fetchall()
    removed_rows = [row for row in existing_rows if row["animal_id"] not in api_ids]
    if not removed_rows:
        return 0

    removed_at = utc_now()
    for row in removed_rows:
        snapshot = dict(row)
        conn.execute(
            """
            INSERT INTO cat_removals (
                animal_id, removed_at, detected_in_run_id, last_seen_at,
                last_known_animal_update, last_known_album_update,
                last_known_album_url, snapshot_json, removal_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["animal_id"],
                removed_at,
                run_id,
                row["last_seen_at"] or row["synced_at"] or removed_at,
                row["source_animal_update"],
                row["source_album_update"],
                row["source_album_url"],
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                "missing_from_upstream_snapshot",
            ),
        )
        local_image = row["local_image"]
        if local_image:
            img_path = IMAGE_DIR / local_image
            if img_path.exists():
                img_path.unlink()
        conn.execute("DELETE FROM image_fetch_state WHERE animal_id = ?", (row["animal_id"],))
        conn.execute("DELETE FROM cats WHERE animal_id = ?", (row["animal_id"],))

    conn.commit()
    log.info("Deleted %d removed animals from DB and recorded history", len(removed_rows))
    return len(removed_rows)


def sync_images(
    conn: sqlite3.Connection,
    upstream: UpstreamClient,
    config: SyncConfig,
    run_id: int,
) -> dict[str, int]:
    eligible_rows = conn.execute(
        """
        SELECT c.animal_id, c.source_album_url, c.local_image, s.failure_count
        FROM cats c
        JOIN image_fetch_state s ON s.animal_id = c.animal_id
        WHERE c.source_album_url IS NOT NULL
          AND c.source_album_url != ''
          AND s.status IN ('pending', 'cooldown')
          AND (s.next_eligible_at IS NULL OR s.next_eligible_at <= ?)
        ORDER BY c.animal_id
        LIMIT ?
        """,
        (utc_now(), config.max_images_per_run),
    ).fetchall()

    log.info("Images to download this run: %d", len(eligible_rows))
    attempted = succeeded = failed = 0

    for row in eligible_rows:
        attempted += 1
        animal_id = row["animal_id"]
        image_url = row["source_album_url"]
        result = upstream.download_image(image_url)
        attempted_at = utc_now()

        conn.execute(
            """
            UPDATE image_fetch_state
            SET last_attempt_at = ?
            WHERE animal_id = ?
            """,
            (attempted_at, animal_id),
        )

        if result.outcome == "success" and result.content is not None:
            dest = IMAGE_DIR / f"{animal_id}.png"
            dest.write_bytes(result.content)
            conn.execute(
                "UPDATE cats SET local_image = ? WHERE animal_id = ?",
                (dest.name, animal_id),
            )
            conn.execute(
                """
                UPDATE image_fetch_state
                SET last_success_at = ?,
                    failure_count = 0,
                    last_error_code = NULL,
                    next_eligible_at = NULL,
                    status = 'success'
                WHERE animal_id = ?
                """,
                (attempted_at, animal_id),
            )
            succeeded += 1
        elif result.outcome == "terminal_failure":
            conn.execute(
                """
                UPDATE image_fetch_state
                SET failure_count = failure_count + 1,
                    last_error_code = ?,
                    next_eligible_at = NULL,
                    status = 'terminal_failure'
                WHERE animal_id = ?
                """,
                (result.error_code, animal_id),
            )
            failed += 1
        else:
            next_eligible_at = (
                datetime.now(timezone.utc) + timedelta(seconds=config.image_failure_cooldown)
            ).isoformat()
            conn.execute(
                """
                UPDATE image_fetch_state
                SET failure_count = failure_count + 1,
                    last_error_code = ?,
                    next_eligible_at = ?,
                    status = 'cooldown'
                WHERE animal_id = ?
                """,
                (result.error_code, next_eligible_at, animal_id),
            )
            failed += 1

        if attempted % 10 == 0:
            conn.commit()
            update_sync_run(
                run_id,
                images_attempted=attempted,
                images_succeeded=succeeded,
                images_failed=failed,
            )
            log.info(
                "  Image progress attempted=%d succeeded=%d failed=%d",
                attempted,
                succeeded,
                failed,
            )

    conn.commit()
    return {"attempted": attempted, "succeeded": succeeded, "failed": failed}


def create_sync_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        INSERT INTO sync_runs (started_at, phase, status)
        VALUES (?, 'full', 'running')
        """,
        (utc_now(),),
    )
    conn.commit()
    return int(cursor.lastrowid)


def update_sync_run(run_id: int, **fields):
    if not fields:
        return
    conn = get_connection()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    params = list(fields.values()) + [run_id]
    conn.execute(f"UPDATE sync_runs SET {assignments} WHERE id = ?", params)
    conn.commit()
    conn.close()


def finalize_sync_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    error_summary: str | None = None,
    images_attempted: int | None = None,
    images_succeeded: int | None = None,
    images_failed: int | None = None,
):
    conn.execute(
        """
        UPDATE sync_runs
        SET finished_at = ?,
            status = ?,
            images_attempted = COALESCE(?, images_attempted),
            images_succeeded = COALESCE(?, images_succeeded),
            images_failed = COALESCE(?, images_failed),
            error_summary = ?
        WHERE id = ?
        """,
        (
            utc_now(),
            status,
            images_attempted,
            images_succeeded,
            images_failed,
            error_summary,
            run_id,
        ),
    )
    conn.commit()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    sync()
