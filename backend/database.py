import os
import shutil
import sqlite3
from pathlib import Path

DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/cats.db")
IMAGE_ROOT = Path(os.path.dirname(DATABASE_PATH)) / "images"
BOOTSTRAP_DATASET_VERSION = "bootstrap"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def dataset_image_dir(dataset_version: str) -> Path:
    return IMAGE_ROOT / dataset_version


def get_current_published_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM app_state WHERE key = 'current_published_version'"
    ).fetchone()
    if row and row["value"]:
        return str(row["value"])
    return None


def set_current_published_version(conn: sqlite3.Connection, dataset_version: str):
    conn.execute(
        """
        INSERT INTO app_state (key, value)
        VALUES ('current_published_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (dataset_version,),
    )


def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)

    conn = get_connection()
    with conn:
        conn.execute(
            """
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
                source_album_url  TEXT,
                source_animal_update TEXT,
                source_album_update TEXT,
                area_pkid         INTEGER,
                shelter_pkid      INTEGER,
                synced_at         TEXT,
                first_seen_at     TEXT,
                last_seen_at      TEXT
            )
            """
        )
        _ensure_column(conn, "cats", "source_album_url", "TEXT")
        _ensure_column(conn, "cats", "source_animal_update", "TEXT")
        _ensure_column(conn, "cats", "source_album_update", "TEXT")
        _ensure_column(conn, "cats", "first_seen_at", "TEXT")
        _ensure_column(conn, "cats", "last_seen_at", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_cats (
                dataset_version   TEXT NOT NULL,
                animal_id         INTEGER NOT NULL,
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
                source_album_url  TEXT,
                source_animal_update TEXT,
                source_album_update TEXT,
                area_pkid         INTEGER,
                shelter_pkid      INTEGER,
                synced_at         TEXT,
                first_seen_at     TEXT,
                last_seen_at      TEXT,
                PRIMARY KEY (dataset_version, animal_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dataset_cats_version
            ON dataset_cats (dataset_version, animal_id DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_image_fetch_state (
                dataset_version   TEXT NOT NULL,
                animal_id         INTEGER NOT NULL,
                source_url_hash   TEXT NOT NULL,
                last_attempt_at   TEXT,
                last_success_at   TEXT,
                failure_count     INTEGER NOT NULL DEFAULT 0,
                last_error_code   TEXT,
                status            TEXT NOT NULL
                    CHECK (status IN ('pending', 'success', 'reused', 'missing')),
                PRIMARY KEY (dataset_version, animal_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_runs (
                id                INTEGER PRIMARY KEY,
                dataset_version   TEXT,
                started_at        TEXT NOT NULL,
                finished_at       TEXT,
                phase             TEXT NOT NULL,
                status            TEXT NOT NULL
                    CHECK (status IN ('running', 'success', 'failed', 'partial')),
                pages_fetched     INTEGER NOT NULL DEFAULT 0,
                records_seen      INTEGER NOT NULL DEFAULT 0,
                records_upserted  INTEGER NOT NULL DEFAULT 0,
                records_removed   INTEGER NOT NULL DEFAULT 0,
                images_attempted  INTEGER NOT NULL DEFAULT 0,
                images_succeeded  INTEGER NOT NULL DEFAULT 0,
                images_failed     INTEGER NOT NULL DEFAULT 0,
                error_summary     TEXT
            )
            """
        )
        _ensure_column(conn, "sync_runs", "dataset_version", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key               TEXT PRIMARY KEY,
                value             TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cat_removals (
                id                      INTEGER PRIMARY KEY,
                animal_id               INTEGER NOT NULL,
                removed_at              TEXT NOT NULL,
                detected_in_run_id      INTEGER NOT NULL,
                last_seen_at            TEXT NOT NULL,
                last_known_animal_update TEXT,
                last_known_album_update TEXT,
                last_known_album_url    TEXT,
                snapshot_json           TEXT NOT NULL,
                removal_reason          TEXT NOT NULL,
                FOREIGN KEY(detected_in_run_id) REFERENCES sync_runs(id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cat_removals_animal_id_removed_at
            ON cat_removals (animal_id, removed_at DESC)
            """
        )
        _bootstrap_published_dataset(conn)
    conn.close()


def _bootstrap_published_dataset(conn: sqlite3.Connection):
    if get_current_published_version(conn):
        return

    legacy_rows = conn.execute("SELECT * FROM cats").fetchall()
    if not legacy_rows:
        return

    bootstrap_dir = dataset_image_dir(BOOTSTRAP_DATASET_VERSION)
    bootstrap_dir.mkdir(parents=True, exist_ok=True)

    for row in legacy_rows:
        row_dict = dict(row)
        local_image = row_dict.get("local_image")
        if local_image:
            src = IMAGE_ROOT / local_image
            dest = bootstrap_dir / local_image
            if src.exists() and not dest.exists():
                shutil.copyfile(src, dest)

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
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                BOOTSTRAP_DATASET_VERSION,
                row_dict["animal_id"],
                row_dict["animal_subid"],
                row_dict["animal_place"],
                row_dict["animal_variety"],
                row_dict["animal_sex"],
                row_dict["animal_bodytype"],
                row_dict["animal_colour"],
                row_dict["animal_age"],
                row_dict["animal_sterilization"],
                row_dict["animal_bacterin"],
                row_dict["animal_foundplace"],
                row_dict["animal_status"],
                row_dict["animal_remark"],
                row_dict["animal_opendate"],
                row_dict["animal_closeddate"],
                row_dict["animal_update"],
                row_dict["animal_createtime"],
                row_dict["shelter_name"],
                row_dict["shelter_address"],
                row_dict["shelter_tel"],
                row_dict["album_file"],
                row_dict["local_image"],
                row_dict["source_album_url"],
                row_dict["source_animal_update"],
                row_dict["source_album_update"],
                row_dict["area_pkid"],
                row_dict["shelter_pkid"],
                row_dict["synced_at"],
                row_dict["first_seen_at"],
                row_dict["last_seen_at"],
            ),
        )

    set_current_published_version(conn, BOOTSTRAP_DATASET_VERSION)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
