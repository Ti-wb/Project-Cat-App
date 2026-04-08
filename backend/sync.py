"""
sync.py - Sync cat data from Taiwan government API to a staged dataset version.
Usage: python sync.py
"""
import hashlib
import ipaddress
import json
import logging
import os
import random
import shutil
import socket
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from database import (
    DATABASE_PATH,
    dataset_image_dir,
    get_connection,
    get_current_published_version,
    init_db,
    set_current_published_version,
)

# ── Configuration ──────────────────────────────────────────────────────────────
IMAGE_ROOT = Path(os.path.dirname(DATABASE_PATH)) / "images"
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
MAX_IMAGE_REDIRECTS = 3
DEFAULT_ALLOWED_IMAGE_HOSTS = ("data.moa.gov.tw", "www.pet.gov.tw", "asms.coa.gov.tw")
DENIED_IP_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("198.18.0.0/15"),
)

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
    max_image_redirects: int = MAX_IMAGE_REDIRECTS


@dataclass(frozen=True)
class DownloadResult:
    outcome: str
    error_code: str | None = None
    content: bytes | None = None


class UnsafeImageUrlError(RuntimeError):
    pass


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
                response = self._get_validated_image(url)
            except UnsafeImageUrlError as exc:
                return DownloadResult(outcome="terminal_failure", error_code=str(exc))
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

            if 300 <= response.status_code < 400:
                response.raise_for_status()
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

    def _get_validated_image(self, url: str) -> httpx.Response:
        current_url = self._validate_image_url(url)
        for _ in range(self.config.max_image_redirects + 1):
            response = self.client.get(current_url, timeout=self.config.request_timeout)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            current_url = self._validate_image_url(urljoin(str(current_url), location))
        raise UnsafeImageUrlError("REDIRECT_LIMIT_EXCEEDED")

    def _validate_image_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise UnsafeImageUrlError("INVALID_URL_SCHEME")
        if not parsed.hostname:
            raise UnsafeImageUrlError("MISSING_URL_HOST")
        try:
            port = parsed.port
        except ValueError as exc:
            raise UnsafeImageUrlError("INVALID_URL_PORT") from exc
        hostname = parsed.hostname.rstrip(".").lower()
        if not self._is_allowed_image_host(hostname):
            raise UnsafeImageUrlError("DISALLOWED_IMAGE_HOST")
        self._ensure_public_host(hostname, port, parsed.scheme)
        return parsed.geturl()

    @staticmethod
    def _is_allowed_image_host(hostname: str) -> bool:
        configured_hosts = os.getenv("IMAGE_HOST_ALLOWLIST")
        if configured_hosts:
            allowed_hosts = tuple(
                host.strip().lower() for host in configured_hosts.split(",") if host.strip()
            )
        else:
            allowed_hosts = DEFAULT_ALLOWED_IMAGE_HOSTS
        return any(hostname == host or hostname.endswith(f".{host}") for host in allowed_hosts)

    @staticmethod
    def _ensure_public_host(hostname: str, port: int | None, scheme: str):
        resolve_port = port or (443 if scheme == "https" else 80)
        try:
            addrinfos = socket.getaddrinfo(hostname, resolve_port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeImageUrlError("HOST_RESOLUTION_FAILED") from exc

        for _, _, _, _, sockaddr in addrinfos:
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
                or any(ip in network for network in DENIED_IP_NETWORKS)
            ):
                raise UnsafeImageUrlError("DISALLOWED_IMAGE_IP")


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
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    init_db()

    conn = get_connection()
    published_version = get_current_published_version(conn)
    dataset_version = generate_dataset_version()
    stage_dir = dataset_image_dir(dataset_version)
    stage_dir.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": USER_AGENT}
    limiter = RateLimiter({"metadata": METADATA_INTERVAL, "image": IMAGE_INTERVAL})
    config = SyncConfig()
    run_id = create_sync_run(conn, dataset_version)

    try:
        with httpx.Client(headers=headers, follow_redirects=False) as client:
            upstream = UpstreamClient(client=client, limiter=limiter, config=config)
            log.info("Fetching data from government API ...")
            records = fetch_all_cats(upstream, run_id)
            log.info("Total records from API: %d", len(records))

            counts = stage_cats(conn, records, run_id, dataset_version, published_version)
            if counts["invalid_records"] > 0:
                error_summary = (
                    f"Skipped publish because {counts['invalid_records']} upstream records "
                    "had missing or invalid animal_id values"
                )
                log.warning(error_summary)
                finalize_sync_run(
                    run_id,
                    status="partial",
                    error_summary=error_summary,
                )
                cleanup_dataset_version(conn, dataset_version)
                return

            removed_count = record_removed_cats(
                conn,
                dataset_version,
                published_version,
                counts["api_ids"],
                run_id,
            )
            update_sync_run(
                run_id,
                records_upserted=counts["added"] + counts["updated"],
                records_removed=removed_count,
            )

            image_stats = sync_images(
                conn,
                upstream,
                dataset_version,
                published_version,
                run_id,
            )

            publish_dataset(conn, dataset_version)
            prune_datasets(conn, keep_versions={dataset_version})
            finalize_sync_run(
                run_id,
                status="success",
                images_attempted=image_stats["attempted"],
                images_succeeded=image_stats["succeeded"],
                images_failed=image_stats["failed"],
            )

            log.info(
                "Sync complete — added: %d, updated: %d, removed: %d, images attempted: %d, images downloaded: %d, image fallbacks/missing: %d",
                counts["added"],
                counts["updated"],
                removed_count,
                image_stats["attempted"],
                image_stats["succeeded"],
                image_stats["failed"],
            )
    except Exception as exc:
        conn.rollback()
        cleanup_dataset_version(conn, dataset_version)
        finalize_sync_run(
            run_id,
            status="failed",
            error_summary=str(exc),
        )
        raise
    finally:
        conn.close()


def stage_cats(
    conn: sqlite3.Connection,
    records: list[dict],
    run_id: int,
    dataset_version: str,
    published_version: str | None,
) -> dict[str, int | set[int]]:
    now = utc_now()
    api_ids: set[int] = set()
    added = updated = invalid_records = 0

    for record in records:
        animal_id = _safe_int(record.get("animal_id"))
        if not animal_id:
            invalid_records += 1
            continue
        api_ids.add(animal_id)

        published_row = get_published_cat(conn, published_version, animal_id)
        local_image = None
        if can_reuse_published_image(published_row, record):
            local_image = copy_published_image(conn, published_version, animal_id, dataset_version)

        row = {
            "dataset_version": dataset_version,
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
            "local_image": local_image,
            "source_album_url": record.get("album_file"),
            "source_animal_update": record.get("animal_update"),
            "source_album_update": record.get("album_update"),
            "area_pkid": _safe_int(record.get("animal_area_pkid")),
            "shelter_pkid": _safe_int(record.get("animal_shelter_pkid")),
            "synced_at": now,
            "first_seen_at": (
                published_row["first_seen_at"] if published_row and published_row["first_seen_at"] else now
            ),
            "last_seen_at": now,
        }

        conn.execute(
            """
            INSERT INTO dataset_cats (
                dataset_version, animal_id, animal_subid, animal_place, animal_variety,
                animal_sex, animal_bodytype, animal_colour, animal_age,
                animal_sterilization, animal_bacterin, animal_foundplace,
                animal_status, animal_remark, animal_opendate, animal_closeddate,
                animal_update, animal_createtime, shelter_name, shelter_address,
                shelter_tel, album_file, local_image, source_album_url,
                source_animal_update, source_album_update, area_pkid, shelter_pkid,
                synced_at, first_seen_at, last_seen_at
            ) VALUES (
                :dataset_version, :animal_id, :animal_subid, :animal_place, :animal_variety,
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

        if published_row:
            updated += 1
        else:
            added += 1

        if should_queue_image_download(published_row, row):
            queue_image_fetch(conn, dataset_version, animal_id, row["source_album_url"])

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


def get_published_cat(
    conn: sqlite3.Connection, published_version: str | None, animal_id: int
) -> sqlite3.Row | None:
    if not published_version:
        return None
    return conn.execute(
        """
        SELECT *
        FROM dataset_cats
        WHERE dataset_version = ? AND animal_id = ?
        """,
        (published_version, animal_id),
    ).fetchone()


def can_reuse_published_image(published_row: sqlite3.Row | None, record: dict) -> bool:
    if not published_row or not published_row["local_image"]:
        return False
    source_album_url = record.get("album_file")
    if not source_album_url:
        return False
    if published_row["album_file"] != source_album_url:
        return False
    if published_row["source_album_update"] != record.get("album_update"):
        return False
    return published_image_path(str(published_row["dataset_version"]), published_row["local_image"]).exists()


def should_queue_image_download(published_row: sqlite3.Row | None, row: dict) -> bool:
    source_album_url = row["source_album_url"]
    if not source_album_url:
        return False
    if row["local_image"]:
        return False
    if not published_row:
        return True
    return True


def queue_image_fetch(conn: sqlite3.Connection, dataset_version: str, animal_id: int, source_url: str):
    source_url_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO dataset_image_fetch_state (
            dataset_version, animal_id, source_url_hash, last_attempt_at, last_success_at,
            failure_count, last_error_code, status
        ) VALUES (?, ?, ?, NULL, NULL, 0, NULL, 'pending')
        """,
        (dataset_version, animal_id, source_url_hash),
    )


def record_removed_cats(
    conn: sqlite3.Connection,
    dataset_version: str,
    published_version: str | None,
    api_ids: set[int],
    run_id: int,
) -> int:
    if not published_version:
        return 0

    existing_rows = conn.execute(
        "SELECT * FROM dataset_cats WHERE dataset_version = ?",
        (published_version,),
    ).fetchall()
    removed_rows = [row for row in existing_rows if row["animal_id"] not in api_ids]
    if not removed_rows:
        return 0

    removed_at = utc_now()
    for row in removed_rows:
        snapshot = dict(row)
        snapshot["replacement_dataset_version"] = dataset_version
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

    conn.commit()
    log.info("Recorded %d removed animals for new dataset %s", len(removed_rows), dataset_version)
    return len(removed_rows)


def sync_images(
    conn: sqlite3.Connection,
    upstream: UpstreamClient,
    dataset_version: str,
    published_version: str | None,
    run_id: int,
) -> dict[str, int]:
    queued_rows = conn.execute(
        """
        SELECT c.animal_id, c.source_album_url
        FROM dataset_cats c
        JOIN dataset_image_fetch_state s
          ON s.dataset_version = c.dataset_version
         AND s.animal_id = c.animal_id
        WHERE c.dataset_version = ?
          AND s.status = 'pending'
        ORDER BY c.animal_id
        """,
        (dataset_version,),
    ).fetchall()

    log.info("Images to resolve for dataset %s: %d", dataset_version, len(queued_rows))
    attempted = succeeded = failed = 0

    for row in queued_rows:
        attempted += 1
        animal_id = row["animal_id"]
        result = upstream.download_image(row["source_album_url"])
        attempted_at = utc_now()

        conn.execute(
            """
            UPDATE dataset_image_fetch_state
            SET last_attempt_at = ?, failure_count = failure_count + 1, last_error_code = ?
            WHERE dataset_version = ? AND animal_id = ?
            """,
            (
                attempted_at,
                result.error_code,
                dataset_version,
                animal_id,
            ),
        )

        if result.outcome == "success" and result.content is not None:
            write_dataset_image(dataset_version, animal_id, result.content)
            conn.execute(
                """
                UPDATE dataset_cats
                SET local_image = ?
                WHERE dataset_version = ? AND animal_id = ?
                """,
                (image_filename(animal_id), dataset_version, animal_id),
            )
            conn.execute(
                """
                UPDATE dataset_image_fetch_state
                SET last_success_at = ?, failure_count = 0, last_error_code = NULL, status = 'success'
                WHERE dataset_version = ? AND animal_id = ?
                """,
                (attempted_at, dataset_version, animal_id),
            )
            succeeded += 1
        else:
            reused = copy_published_image(conn, published_version, animal_id, dataset_version)
            status = "reused" if reused else "missing"
            conn.execute(
                """
                UPDATE dataset_cats
                SET local_image = ?
                WHERE dataset_version = ? AND animal_id = ?
                """,
                (reused, dataset_version, animal_id),
            )
            conn.execute(
                """
                UPDATE dataset_image_fetch_state
                SET status = ?
                WHERE dataset_version = ? AND animal_id = ?
                """,
                (status, dataset_version, animal_id),
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


def publish_dataset(conn: sqlite3.Connection, dataset_version: str):
    set_current_published_version(conn, dataset_version)
    conn.commit()


def prune_datasets(conn: sqlite3.Connection, keep_versions: set[str]):
    versions = {
        row["dataset_version"]
        for row in conn.execute("SELECT DISTINCT dataset_version FROM dataset_cats").fetchall()
    }
    versions.update(
        row["dataset_version"]
        for row in conn.execute(
            "SELECT DISTINCT dataset_version FROM dataset_image_fetch_state"
        ).fetchall()
    )

    prune_versions = versions - keep_versions
    if not prune_versions:
        return

    for dataset_version in prune_versions:
        cleanup_dataset_version(conn, dataset_version)


def cleanup_dataset_version(conn: sqlite3.Connection, dataset_version: str):
    conn.execute(
        "DELETE FROM dataset_image_fetch_state WHERE dataset_version = ?",
        (dataset_version,),
    )
    conn.execute(
        "DELETE FROM dataset_cats WHERE dataset_version = ?",
        (dataset_version,),
    )
    conn.commit()

    image_dir = dataset_image_dir(dataset_version)
    if image_dir.exists():
        shutil.rmtree(image_dir)


def copy_published_image(
    conn: sqlite3.Connection,
    published_version: str | None,
    animal_id: int,
    target_version: str,
) -> str | None:
    if not published_version:
        return None

    row = conn.execute(
        """
        SELECT local_image
        FROM dataset_cats
        WHERE dataset_version = ? AND animal_id = ?
        """,
        (published_version, animal_id),
    ).fetchone()
    if not row or not row["local_image"]:
        return None

    src = published_image_path(published_version, row["local_image"])
    if not src.exists():
        return None

    dest_dir = dataset_image_dir(target_version)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / row["local_image"]
    if src != dest and not dest.exists():
        shutil.copyfile(src, dest)
    return row["local_image"]


def published_image_path(dataset_version: str, local_image: str) -> Path:
    return dataset_image_dir(dataset_version) / local_image


def image_filename(animal_id: int) -> str:
    return f"{animal_id}.png"


def write_dataset_image(dataset_version: str, animal_id: int, content: bytes):
    dest_dir = dataset_image_dir(dataset_version)
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / image_filename(animal_id)).write_bytes(content)


def create_sync_run(conn: sqlite3.Connection, dataset_version: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO sync_runs (dataset_version, started_at, phase, status)
        VALUES (?, ?, 'full', 'running')
        """,
        (dataset_version, utc_now()),
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
    run_id: int,
    status: str,
    error_summary: str | None = None,
    images_attempted: int | None = None,
    images_succeeded: int | None = None,
    images_failed: int | None = None,
):
    conn = get_connection()
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
    conn.close()


def generate_dataset_version() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    sync()
